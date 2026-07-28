# Drive PDF Margin Helper - Design

## Goal

Automatically crop the margins of PDFs (mostly academic papers) so they read
well on a Kindle Scribe, without needing the cropping toolchain on every device.

Workflow: drop a PDF into a OneDrive `upload/` folder from any device. A helper
running on a NixOS server detects it, crops the margins losslessly, and writes
the result to a `processed/` folder. The Scribe imports from `processed/` using
its native OneDrive integration.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Drive provider | OneDrive | Best server ergonomics on NixOS: the abraunegg `onedrive` client has native `--monitor` (inotify) sync, simpler OAuth than Google Drive, and a first-class NixOS module. |
| Detection | Near-realtime | `onedrive --monitor` keeps a local mirror in sync both ways; a separate inotify watcher on the local `upload/` dir triggers processing within seconds. |
| Crop tool | `pdfcropmargins` | Adjusts CropBox/MediaBox only, no re-render: lossless, fast, no file-size bloat (important for the Scribe). Vendored at `nix/pdfcropmargins.nix`. |
| Crop invocation | Via a crop shim (`scribe-crop-shim`) | The service shells out to our shim, which imports and drives `pdfcropmargins`' public `crop()`. Disabled, it is bit-for-bit the bare tool for the same argv; enabled, it composes the header/footer strip into the same single crop. Keeps the subprocess isolation (timeout / OOM / native-crash containment) the daemon relies on. |
| Header/footer strip | Opt-in, default off | Detect and trim running headers/footers losslessly (box rewrite only) by cross-page positional recurrence, abstaining whenever not confident. First profile key that is a shim directive, not a `pdfcropmargins` flag. See `docs/header-footer-strip.md`. |
| Reader-fit sizing | On by default | The shim owns the final per-page boxes: one shared box per document (outlier pages deviate individually), floored so the on-device magnification never exceeds `fit_max_scale`. Replaces `uniform`/`same_size`, which are removed from the schema. See `docs/reader-fit.md`. |
| Crop config | Auto-crop default + per-file sidecar overrides | One sensible profile for everything; optional `<name>.pdf.toml` sidecar for documents that need tuning. |
| Originals | Non-destructive | Original stays in `upload/`; cropped copy written to `processed/` at the same relative path. Re-runnable if settings change. |
| Output integrity | Atomic publish | Outputs are written to a temp file and atomically renamed into place; the processor is the sole writer of `processed/`/`failed/`. Prevents the sync client uploading half-written files or creating conflict copies. |
| Deletions | Mirror into outputs | Removing a source from `upload/` causes its `processed/`/`failed/` outputs to be removed on the next reconcile, so the Scribe library and `processed/` don't accumulate orphans. |
| Failures | Move to `failed/` + log | Failed inputs go to `failed/` with a `.log`; visible from the drive/Scribe side, keeps `upload/` clean. |
| Implementation | Python | `pdfcropmargins` is Python; clean config parsing, retries, structured logging, inotify via `watchdog`. |
| Python deps | `uv` | Project managed with `uv` (lockfile, venv). `pdfcropmargins` is now a declared Python runtime dependency (the shim imports it, not just shells out) and pulls in PyMuPDF transitively. `onedrive`/`ghostscript` and the `pdfcropmargins` binary still come from Nix. |
| Deployment | NixOS | `services.onedrive` module + a systemd service for the watcher. |

## Architecture

```
   any device                OneDrive cloud                NixOS server
  ┌──────────┐              ┌──────────────┐         ┌────────────────────────┐
  │ drop PDF ├──────────────► ScribeCrop/  │◄───────►│ onedrive --monitor     │
  │ in upload│              │   upload/    │  sync   │   (local mirror)       │
  └──────────┘              │   processed/ │         │          │             │
                            │   failed/    │         │   inotify│ watch       │
   ┌─────────┐              │              │         │          ▼             │
   │ Scribe  │◄─────────────┤              │         │  scribe-crop service   │
   │ import  │   native     │              │         │  (watcher + processor) │
   └─────────┘   OneDrive   └──────────────┘         │          │             │
                                                     │  pdfcropmargins (PATH) │
                                                     └────────────────────────┘
```

Two cooperating daemons on the server:

1. **`onedrive --monitor`** (abraunegg client, `services.onedrive`): keeps the
   local mirror of the `ScribeCrop/` subtree in sync with OneDrive in both
   directions. Selective sync (`sync_list`) limits it to that subtree. When a
   new upload appears in the cloud it lands in the local `upload/` dir; when the
   processor writes to the local `processed/`/`failed/` dirs the client uploads
   them.

2. **`scribe-crop`** (this project): watches the local `upload/` dir, crops new
   or changed PDFs, and writes outputs to the local `processed/` or `failed/`
   dirs. It never talks to OneDrive directly; the mirror is its whole world. The
   crop step shells out to a sibling console script, **`scribe-crop-shim`**,
   which drives `pdfcropmargins` and (when enabled) the header/footer strip; the
   subprocess boundary contains timeouts, OOM, and native crashes.

This separation keeps the helper provider-agnostic in spirit (it only touches a
local directory tree) and leans on a mature client for sync.

### Contract with the sync client

The local mirror is bidirectional, so the boundary between the two daemons must
be explicit to avoid corruption, conflict copies, and loops:

- **`scribe-crop` is the sole writer of `processed/` and `failed/`.** Nothing
  else is expected to write there. Cloud-origin changes under those dirs (e.g. a
  file edited on another device) are not treated as inputs and never trigger
  processing; only `upload/` events and the root `config.toml` do.
- **All outputs are published atomically:** written to a temp file on the same
  filesystem, then `rename(2)`d into the final path under `processed/`/`failed/`.
  The sync client therefore only ever sees a complete file appear, never a
  partially written one, and never races us mid-write (which is what produces
  `name-1.pdf` conflict copies that the Scribe would import as duplicates).
  `rename(2)` is atomic only within one mount, so the temp dir must share the
  mount with the output dirs; if a configurable temp location is ever introduced,
  it must be validated against the output mount (else fall back to a non-atomic
  copy, losing this guarantee). Temp files are written to a dedicated scratch dir
  (`<root>/.scribe-crop-tmp/`) that shares the root mount but sits outside the
  synced content dirs, so transient `.tmp` files never appear under
  `upload/`/`processed/`/`failed/`. The drive sync config must exclude this
  scratch dir (it is not part of the synced subtree).
- **Inputs rely on the abraunegg client's atomic-move semantics.** That client
  downloads to a temporary name and `rename`s the finished file into place, so
  the watcher's primary trigger is a move into `upload/`. The size-stability
  check is a secondary guard for editors/clients that write in place, not the
  primary correctness mechanism.
- **Log/marker files** (`config.error.log`, `failed/<name>.pdf.log`) are written
  only by the service and are never inputs: the watcher triggers solely on
  `*.pdf`/`*.pdf.toml` under `upload/` and on the root `config.toml`, so logs
  syncing back to the cloud cannot cause reprocessing.

## Folder layout

In OneDrive, mirrored locally under a configured root (e.g. `~/OneDrive/ScribeCrop`):

```
ScribeCrop/
  config.toml    # optional global config, editable from any device (see below)
  config.error.log  # written by the service iff config.toml fails to parse/validate
  upload/        # you drop PDFs here (source of truth, untouched)
  processed/     # cropped output, mirroring upload/'s relative paths
  failed/        # failed inputs (copied here) + <name>.pdf.log, same relative paths
```

`upload/` may contain subdirectories. Outputs preserve the source's relative
path: `upload/papers/foo.pdf` -> `processed/papers/foo.pdf`. This avoids
collisions between same-named files in different subdirectories, and keeps the
state key (source relative path) and the output path in one-to-one
correspondence.

Sidecar override files live next to their PDF in `upload/`:

```
upload/
  attention-is-all-you-need.pdf
  attention-is-all-you-need.pdf.toml   # optional per-file overrides
```

## Crop profile

### Default profile

Applied to every PDF unless overridden. Composed from built-in defaults
overlaid with the drive `config.toml [crop]` table (see Configuration). Tuned
for single-column-friendly reading on the Scribe with consistent page sizes.
Built-in defaults:

- `-p 10` retain 10% of existing margins (avoids clipping descenders/superscripts)
- `fit_scope = "document"`: one shared box for the document, so the reader's zoom
  does not jitter between pages
- `fit_reader = true` with `fit_max_scale = 1.15`: never magnify a page past 1.15x
  its printed size on the configured screen
- `fit_exclude_first_page = true`: page 0 does not vote in the shared-box
  aggregation, so a badge or full-bleed cover does not loosen every other page

Rationale: a maximal crop is right for letter-size papers (the on-device scale
stays below 1, so every point of margin helps) but wrong for small-trim books,
where it balloons the page past its printed size. Because PDF units are physical,
bounding the magnification against the configured screen expresses that in one
continuous geometric rule, with no per-document configuration and no document
classification. Consistent page geometry is the default because per-page cropping
makes the reader's zoom visibly jitter between pages; `fit_scope = "page"` restores
per-page boxes for documents that want them.

The crop shim owns the final boxes for this: it sees every page's tight box and
full page box before the crop math runs, so the floor, the shared box, the
first-page exemption, and the header/footer strip all compose in one pass. The
`uniform`/`same_size` keys (`-u`/`-s`) are therefore **not** in the schema; their
min-delta semantics cannot express the floor, and they would post-process the
boxes the shim injects. See `docs/reader-fit.md` for the full behavior spec, the
decision record, and the rejected alternatives.

### Per-file sidecar overrides

Optional TOML file `<name>.pdf.toml` next to the source PDF. Highest precedence;
absent keys fall back to the effective default profile (built-in defaults
overlaid with the drive `config.toml`). Schema (keys map to `pdfcropmargins`
flags, except the shim directives noted below):

```toml
percent_retain   = 15          # -p PCT          (single value)
percent_retain4  = [50,20,40,10] # -p4 L B R T   (overrides percent_retain)
absolute4        = [0,0,12,0]  # -a4 L B R T   (bp to crop; negative adds space)
pre_crop         = 5           # -ap BP        (pre-crop before bbox detect; scanned/noisy)
threshold        = 191         # -t BYTEVAL    (background detection threshold)
use_ghostscript  = true        # -gs           (ghostscript bbox detection)
pages            = "2-"        # -g PAGESTR    (restrict cropped pages)
password         = "secret"    # -pw PASSWD    (encrypted input)
strip_header_footer = true     # detect+trim running header/footer (default false)
fit_reader       = true        # bound on-device magnification (default true)
fit_max_scale    = 1.15        # the magnification cap (default 1.15)
fit_scope        = "document"  # "document" (shared box) or "page" (default "document")
fit_exclude_first_page = true  # document scope: page 0 does not vote (default true)
```

Validation: unknown keys are rejected (fail the file rather than silently
ignore). The mapping from TOML keys to CLI flags lives in one place. `uniform`
and `same_size` were removed with reader-fit and are now rejected as unknown
keys; document-wide consistency is `fit_scope = "document"` instead.

`strip_header_footer` and the four `fit_*` keys are the profile keys that are
**not** `pdfcropmargins` CLI flags; they are directives to the crop shim that
wraps `pdfcropmargins`. They are validated and merged through the same precedence
path (built-in < drive `config.toml [crop]` < per-file sidecar) but are never
emitted to the `pdfcropmargins` argv.

When `strip_header_footer` is on, the shim detects the document's running header
and footer (by cross-page positional recurrence of an isolated edge line) and
trims them as part of the same single, lossless crop, abstaining whenever it is
not confident. See `docs/header-footer-strip.md` for the algorithm and the
constants.

The `fit_*` keys drive the reader-fit pipeline: `fit_scope` chooses one shared
box per document or per-page boxes, `fit_reader`/`fit_max_scale` bound the
on-device magnification against the screen size from the server config's
`[reader]` table, and `fit_exclude_first_page` keeps a first-page artifact from
widening the shared box. Content safety is absolute in every mode: a page whose
own content does not fit the shared box gets that box minimally expanded for
itself alone, so no page ever loses ink. See `docs/reader-fit.md` for the
normative nine-step behavior spec.

Fingerprint interaction: each shim directive folds one token into the dedup key,
and only when it is in force, so a file with a feature off keys identically to a
run without the feature at all. The strip token is a `DETECTOR_VERSION` plus the
strip constants. The reader-fit token carries `fit_scope` always,
`fit_exclude_first_page` only under document scope, and the screen dimensions
plus `fit_max_scale` only when `fit_reader` is true, so bumping `fit_max_scale`
does not re-crop files that have the floor disabled. PyMuPDF (which the shim
reads text geometry through) is deliberately **not** in the key and not a
declared dependency: it rides transitively on `pdfcropmargins`' pinned version,
so the existing toolchain token covers it, the same stance the whitespace crop
already takes.

## Processing algorithm

For each candidate PDF at relative path `<relpath>` under `upload/`:

1. **Stability check.** Confirm the file is settled before reading it (see the
   sync contract: the move-into-place is the primary signal; size-stable-for-a-
   short-window is the secondary guard against in-place writers). A stalled and
   resumed download is handled by the fingerprint: if bytes change later, the
   later event re-enqueues and reprocesses.
2. **Resolve the effective profile, then fingerprint.** Compose the effective
   profile (built-in < `config.toml [crop]` < sidecar) and reduce it to its
   `pdfcropmargins` argv. The dedup key is
   `hash(pdf bytes + profile argv + tool_version)`, where `tool_version` is the
   resolved `pdfcropmargins`/`ghostscript` version so a toolchain upgrade re-crops
   automatically. Keying on the resolved argv means any layer change (built-in
   defaults, drive config, sidecar) that alters the crop flags re-crops
   automatically with no manual version bump, while a change that leaves the flags
   identical (a comment-only config edit) does not. If the state store records
   this exact fingerprint as a success and `processed/<relpath>` exists, skip.
   A malformed sidecar/profile has no argv to fingerprint: it is not processed,
   only logged (see below).
3. **Build the command** from the resolved argv, targeting a temp output path on
   the same filesystem as `processed/`. The target is the crop shim
   (`scribe-crop-shim`), not `pdfcropmargins` directly; the resolved
   `strip_header_footer` and reader-fit directives are passed to the shim (and
   folded into the fingerprint) only when they would change the output. The
   reader-fit directive is resolved once, from the crop profile's `fit_*` keys
   plus the server config's `[reader]` screen size, and that one resolution
   drives both the command and the key, so they cannot drift.
4. **Run the crop shim** with a timeout. It drives `pdfcropmargins`' public
   `crop()`: with every feature off it is a pass-through with bit-for-bit
   parity; otherwise it composes the header/footer trim and the reader-fit
   sizing into the same single crop and injects the final boxes directly.
5. **On success:** atomically rename the temp output to `processed/<relpath>`,
   then record the fingerprint as a success. (Publish-then-record ordering: if
   we crash between the two, the next reconcile recomputes the same fingerprint
   and finds the already-published output, so at worst it re-records.) Remove any
   stale `failed/<relpath>` + log for this file.
6. **On failure**, classify before recording. The shim emits a structured exit
   code (content vs environmental) rather than relying on stderr scraping, so a
   detector/PyMuPDF traceback that happens to contain a word like "bounding box"
   is never mistaken for a permanent content suppression:
   - **Content failures** (corrupt PDF, encrypted-without-password, no detectable
     bounding box): atomically publish a copy of the original to
     `failed/<relpath>` plus `failed/<relpath>.pdf.log`, and record the failure
     fingerprint so the same bytes are not retried forever. These will not
     succeed without the user changing the input or its sidecar.
   - **Malformed sidecar/profile** (unparseable `.pdf.toml`, unknown crop key):
     detected before the run, so there is no effective argv to fingerprint and
     nothing to crop. Write only `failed/<relpath>.pdf.log` (no PDF copy) so the
     reason is visible on any device, and clear any stale outputs. No suppression
     fingerprint is recorded; the file is re-checked each pass and fails fast
     (before `pdfcropmargins` runs). The log write is idempotent so a repeatedly
     re-checked file does not churn the sync. An empty-fingerprint state row is
     recorded only so reverse-GC removes the log once the source is deleted.
   - **Environmental failures** (timeout, OOM, missing/again ghostscript, mid-run
     crash, disk full): do **not** persist a permanent suppression. Retry with
     bounded backoff; surface the condition in the service log. These are
     expected to recover without the user perturbing the bytes.

Arrival ordering: a `<name>.pdf.toml` event maps to `<name>.pdf` and enqueues it;
if the PDF has not arrived yet the enqueue is a harmless no-op and the PDF's own
later event (or the reconcile scan) processes it once both are present.

## Idempotency & recovery

- **State store:** a local SQLite database (stdlib `sqlite3`, no extra dep)
  mapping source relative path -> last processed fingerprint + outcome +
  timestamps. SQLite is chosen over a JSON file for atomic, crash-safe updates
  and easy querying by path/outcome. Survives restarts.
- **Startup reconcile (bidirectional):** on service start (and after a
  `config.toml` change), scan in both directions:
  - *Forward:* for each PDF in `upload/`, process anything whose fingerprint
    differs from the recorded one (catches events missed while the service was
    down).
  - *Reverse (GC):* remove an output and its state record only for a source that
    was **previously seen** (has a state record) and is **now gone** from
    `upload/`, i.e. a confirmed deletion. A source that is merely absent right
    now (mirror offline/lagging, selective-sync still fetching, fresh start
    before first sync) is *not* a deletion and must not be GC'd, otherwise
    reconcile could delete valid outputs whose sources simply haven't synced down
    yet and then re-crop them on arrival (churn + a transient gap the Scribe
    might see). GC therefore runs only when the mirror is known-current; the
    forward pass has no such hazard.
  The watcher handles the steady state; reconcile is the missed-event and
  deletion backstop. GC deletions under `processed/`/`failed/` are themselves
  sync writes and are expected to round-trip to the cloud; conversely, an output
  deletion arriving *from* the cloud is not re-created by the service (the source
  in `upload/` drives output existence, not the other way around).
- **Profile / config / toolchain changes:** the fingerprint keys on the effective
  profile's argv, so changing built-in defaults, the drive `config.toml [crop]`,
  or a per-file sidecar re-crops automatically whenever the resolved crop flags
  change; a `pdfcropmargins`/`ghostscript` upgrade changes `tool_version`; both fold
  into the fingerprint, invalidating affected entries so everything re-crops on next
  reconcile. Reprocessing is safe because outputs are deterministic and replace
  the previous file at the same relative path via atomic rename.

## Watcher behavior

- Built on the `watchdog` library (its Linux observer wraps inotify), chosen
  over a raw inotify wrapper for a managed observer thread, recursive watching,
  and a cleaner API. Precise event-type semantics are not required because the
  stability check (below) guards against partial writes regardless of which
  event fired.
- Events of interest on `upload/`: create/modify/move of `*.pdf` and
  `*.pdf.toml`. Any such event enqueues the associated PDF; the stability check
  then waits out in-progress syncs (the abraunegg client downloads to a temp
  name and moves into place, so a move into `upload/` is the common trigger).
- The root `config.toml` is also watched: on change the service reloads the
  effective default profile (with validation/fallback as above) and triggers a
  reconcile pass so default-driven files re-crop.
- Debounce rapid event bursts per path.
- A `.toml` event maps back to its `<name>.pdf` and enqueues that PDF.
- Processing is serialized through a single worker queue (cropping is light;
  ghostscript bbox detection is the heaviest step). Worker count is a config
  value if parallelism is later wanted.

## Configuration

Two layers with a clean boundary: the drive config says *what to do to PDFs*
(user-tunable from any device); the server config says *how the daemon runs*
(operational, set on the machine).

### Drive config (`ScribeCrop/config.toml`)

Optional file at the synced root, editable from any device. Holds only the
default crop profile in a `[crop]` table, using the same keys as the per-file
sidecar schema:

```toml
[crop]
percent_retain  = 8
pre_crop        = 5
```

- **Precedence:** built-in defaults < `config.toml [crop]` < per-file sidecar.
- **Reload:** the watcher also watches this file; on change the service
  recomputes the effective default profile.
- **Reprocessing:** each file's fingerprint keys on its effective profile argv,
  into which `config.toml [crop]` is merged, so editing `config.toml` re-crops
  everything that relied on defaults whenever the resolved flags change.
- **Validation / failure isolation:** unknown keys or a parse error do not crash
  the service. The service keeps using the last-known-good config (or built-in
  defaults on first start) and writes the reason to `ScribeCrop/config.error.log`
  so it is visible from any device. The error file is removed once a valid
  config is loaded.

### Server config (NixOS, not in the drive)

Operational settings, kept off the drive because a bad value could break the
daemon:

- `root`: local mirror root containing `config.toml` + `upload/processed/failed`.
- `stability_seconds`, `process_timeout_seconds`, `worker_count`.
- `max_input_bytes`: inputs larger than this go straight to `failed/` with a log.
- `retry_backoff`: bounds for retrying environmental failures.
- `state_path`: location of the SQLite state store.
- `[reader]`: the reader's screen size, either a built-in `device` preset
  (default `"scribe-colorsoft"`, 6.6 x 8.8 in) or explicit `screen_width_in` /
  `screen_height_in`. The two are mutually exclusive. Screen hardware describes
  the deployment and never changes, so it belongs here rather than in the drive
  config's "what to do to PDFs" surface; the behavioral `fit_*` knobs stay in the
  crop profile. The service logs the resolved screen size at startup so a
  deployment that forgot the table on a different device is visible.

The OneDrive account, auth, and `sync_list` are configured on the `onedrive`
client side (NixOS module), not in this service.

## Deployment (NixOS)

- **OneDrive client:** `services.onedrive` for the target user, `sync_list`
  restricted to the `ScribeCrop/` subtree, running in monitor mode.
- **Cropping toolchain:** `pdfcropmargins` (vendored at `nix/pdfcropmargins.nix`)
  plus `ghostscript` on the service PATH. `pdfcropmargins` wraps `ghostscript` +
  `poppler_utils` for its own children, but the service probes `gs --version`
  directly for the fingerprint, so `ghostscript` is on the service PATH too.
  `pdfcropmargins` is also a Python import dependency of the package (it propagates
  the PyMuPDF the shim reads through), so it is on the import path, not just PATH.
- **`scribe-crop-shim`:** installed as a console script alongside `scribe-crop`
  in the same `bin/`; the daemon resolves it on PATH or as a sibling of its own
  entry point.
- **`scribe-crop` service:** a systemd service running the Python app, ordered
  after the onedrive mirror is available, with the config above. Restart on
  failure.
- The service runs as the same user that owns the local OneDrive mirror so it
  can read `upload/` and write `processed/`/`failed/`.

## Operational considerations

- **Sync-client liveness.** `scribe-crop` cannot distinguish "no new uploads"
  from "the onedrive client is broken" (expired token, `sync_list` misconfig,
  quota exceeded, crashed unit): both look like a quiet `upload/`. The onedrive
  systemd unit is the operator's responsibility to monitor (its own logs/status).
  `scribe-crop` additionally logs a heartbeat noting how long since the last
  observed change, so a silently stalled mirror is at least visible in the log.
- **Disk and memory.** Each kept original is roughly doubled (`upload/` +
  `processed/`), and failures add a third copy under `failed/`. `pdfcropmargins`
  is light when it just rewrites the CropBox, but ghostscript bounding-box
  detection on large scanned PDFs is memory- and CPU-heavy. Mitigations: a
  configurable max input size above which a file is sent to `failed/` with an
  explanatory log rather than processed, and serialized processing (single
  worker by default) to bound peak memory. The reverse-reconcile GC keeps
  `processed/`/`failed/` from growing unbounded as sources are deleted.

## Development

- `uv` manages the Python project (deps, lockfile, venv): `uv init`, `uv add`,
  `uv run`.
- Python dependency footprint is small: `watchdog` and `pdfcropmargins` are the
  third-party runtime deps. The shim imports `pdfcropmargins`, which pulls in
  PyMuPDF transitively; the shim only reaches PyMuPDF through the page objects
  `pdfcropmargins` hands it (no direct import of our own), so PyMuPDF is not
  declared. `sqlite3` (state store) and `tomllib` (sidecar parsing) are stdlib on
  the targeted Python 3.14.
- System tools (`pdfcropmargins`, `onedrive`, `ghostscript`) come from Nix; a
  dev shell provides them on PATH for local testing.
- Local testing does not require OneDrive: point `root` at a scratch directory
  with `upload/processed/failed` and drop files into `upload/`.

## Verification

- **Unit:** TOML sidecar -> CLI flag mapping; fingerprint stability; default
  profile composition with/without overrides.
- **Integration (no cloud):** scratch `root`; drop a known-margin PDF into
  `upload/`, assert a cropped file with a smaller CropBox appears at the same
  relative path under `processed/`; assert sidecar overrides change the result;
  assert a corrupt/encrypted PDF lands in `failed/` with a log; assert
  same-basename files in different subdirs do not collide.
- **Atomic publish:** assert no partially written file ever appears in
  `processed/` (outputs only ever appear via rename); assert the processor
  ignores files placed under `processed/`/`failed/` by something else.
- **Deletions:** removing a source from `upload/` removes its `processed/`/
  `failed/` outputs and state record on the next reconcile.
- **Failure classification:** a content failure is suppressed on identical bytes;
  an environmental failure (simulated timeout / missing tool) retries rather than
  permanently suppressing.
- **Idempotency:** re-running over an unchanged tree produces no new work;
  editing a sidecar reprocesses only that file; a change to the drive
  `config.toml`, the built-in defaults, or `tool_version` that alters the
  resolved crop flags reprocesses all.
- **Config robustness:** a malformed `config.toml` leaves processing on the
  last-known-good profile and produces `config.error.log`; fixing it clears the
  error and reloads.
- **Header/footer strip:** with every shim feature off, shim output is
  bit-for-bit the bare `pdfcropmargins` call; with strip on, a recurring band is
  trimmed while title pages and figure-top pages keep their content (they deviate
  from the shared box individually rather than being clipped). Detector logic is
  unit-tested without PDFs; the shim is tested against the real
  `pdfcropmargins`/PyMuPDF. Full criteria in `docs/header-footer-strip.md`.
- **Reader-fit:** the pipeline's steps are unit-tested as pure geometry (retain
  padding and its internal ordering, cohort selection, aggregation with strip
  caps, the binding-dimension floor, and expand/translate/shrink placement); the
  acceptance criteria are geometric assertions on published boxes, run at the
  shim level against the real tool. A letter paper does not hit the floor and
  every cohort page shares one box; an A5 book grows only its binding dimension
  to exactly `screen/max_scale`; badge pages, mixed-size inserts, and rotated
  pages deviate individually without losing ink. Full criteria in
  `docs/reader-fit.md`.
- **End-to-end:** with the real OneDrive client, drop a PDF from another device,
  confirm it appears cropped in `processed/` and imports cleanly on the Scribe.

## Open questions / future

- Whether to also generate a Scribe-optimized variant (e.g. grayscale flatten)
  for scanned PDFs; out of scope for v1.
- Notifications on failure (push/email) instead of only the `failed/` log.
