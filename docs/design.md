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
| Crop config | Auto-crop default + per-file sidecar overrides | One sensible profile for everything; optional `<name>.pdf.toml` sidecar for documents that need tuning. |
| Originals | Non-destructive | Original stays in `upload/`; cropped copy written to `processed/` at the same relative path. Re-runnable if settings change. |
| Output integrity | Atomic publish | Outputs are written to a temp file and atomically renamed into place; the processor is the sole writer of `processed/`/`failed/`. Prevents the sync client uploading half-written files or creating conflict copies. |
| Deletions | Mirror into outputs | Removing a source from `upload/` causes its `processed/`/`failed/` outputs to be removed on the next reconcile, so the Scribe library and `processed/` don't accumulate orphans. |
| Failures | Move to `failed/` + log | Failed inputs go to `failed/` with a `.log`; visible from the drive/Scribe side, keeps `upload/` clean. |
| Implementation | Python | `pdfcropmargins` is Python; clean config parsing, retries, structured logging, inotify via `watchdog`. |
| Python deps | `uv` | Project managed with `uv` (lockfile, venv). System deps (`pdfcropmargins`, `onedrive`, `ghostscript`) come from Nix. |
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
   dirs. It never talks to OneDrive directly; the mirror is its whole world.

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

- `-p 10` retain 10% of existing margins (the tool default)
- neither `-u` nor `-s`: each page is cropped to its own bounding box

Rationale: cropping per page (no `-u`/`-s`) gives each page its tightest crop,
so pages with wide margins are trimmed more than pages with narrow margins
instead of every page sharing one conservative crop amount. Retaining a small
margin avoids clipping descenders/superscripts. Operators who prefer a stable,
consistent page box across the document (no size jitter between pages on the
Scribe) can set `uniform`/`same_size` from any device via the drive
`config.toml` without touching code or the server.

### Per-file sidecar overrides

Optional TOML file `<name>.pdf.toml` next to the source PDF. Highest precedence;
absent keys fall back to the effective default profile (built-in defaults
overlaid with the drive `config.toml`). Schema (keys map to `pdfcropmargins`
flags):

```toml
percent_retain   = 15          # -p PCT          (single value)
percent_retain4  = [50,20,40,10] # -p4 L B R T   (overrides percent_retain)
uniform          = true        # -u
same_size        = true        # -s
absolute4        = [0,0,12,0]  # -a4 L B R T   (bp to crop; negative adds space)
pre_crop         = 5           # -ap BP        (pre-crop before bbox detect; scanned/noisy)
threshold        = 191         # -t BYTEVAL    (background detection threshold)
use_ghostscript  = true        # -gs           (ghostscript bbox detection)
pages            = "2-"        # -g PAGESTR    (restrict cropped pages)
password         = "secret"    # -pw PASSWD    (encrypted input)
```

Validation: unknown keys are rejected (fail the file rather than silently
ignore). The mapping from TOML keys to CLI flags lives in one place.

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
   the same filesystem as `processed/`.
4. **Run `pdfcropmargins`** with a timeout.
5. **On success:** atomically rename the temp output to `processed/<relpath>`,
   then record the fingerprint as a success. (Publish-then-record ordering: if
   we crash between the two, the next reconcile recomputes the same fingerprint
   and finds the already-published output, so at worst it re-records.) Remove any
   stale `failed/<relpath>` + log for this file.
6. **On failure**, classify before recording:
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

The OneDrive account, auth, and `sync_list` are configured on the `onedrive`
client side (NixOS module), not in this service.

## Deployment (NixOS)

- **OneDrive client:** `services.onedrive` for the target user, `sync_list`
  restricted to the `ScribeCrop/` subtree, running in monitor mode.
- **Cropping toolchain:** `pdfcropmargins` (vendored at `nix/pdfcropmargins.nix`)
  plus `ghostscript` on the service PATH. `pdfcropmargins` wraps `ghostscript` +
  `poppler_utils` for its own children, but the service probes `gs --version`
  directly for the fingerprint, so `ghostscript` is on the service PATH too.
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
- Python dependency footprint is small: `watchdog` is the only third-party
  runtime dep. `sqlite3` (state store) and `tomllib` (sidecar parsing) are
  stdlib on the targeted Python 3.14.
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
- **End-to-end:** with the real OneDrive client, drop a PDF from another device,
  confirm it appears cropped in `processed/` and imports cleanly on the Scribe.

## Open questions / future

- Whether to also generate a Scribe-optimized variant (e.g. grayscale flatten)
  for scanned PDFs; out of scope for v1.
- Notifications on failure (push/email) instead of only the `failed/` log.
