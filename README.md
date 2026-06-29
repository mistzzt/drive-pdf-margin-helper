# scribe-crop

Auto-crop the margins of PDFs so they read well on a Kindle Scribe, without
needing the cropping toolchain on every device.

You drop a PDF into a OneDrive `upload/` folder from any device. A helper running
on a NixOS server detects it, crops the margins losslessly with
[`pdfcropmargins`](https://github.com/abarker/pdfCropMargins) (it only adjusts the
CropBox/MediaBox, so no re-render and no file-size bloat), and writes the result
to a `processed/` folder. The Scribe imports from `processed/` using its native
OneDrive integration.

`scribe-crop` never talks to OneDrive directly. It watches a local directory tree
that the [abraunegg `onedrive`](https://github.com/abraunegg/onedrive) client keeps
in sync. See `docs/design.md` for the full design and the sync-client contract.

## Drive folder layout

Mirrored locally under a configured root (e.g. `~/OneDrive/ScribeCrop`):

```
ScribeCrop/
  config.toml        # optional global crop config, editable from any device
  config.error.log   # written by the service iff config.toml fails to validate
  upload/            # drop PDFs here (source of truth, untouched)
  processed/         # cropped output, mirroring upload/'s relative paths
  failed/            # failed inputs + <name>.pdf.log, same relative paths
  .scribe-crop-tmp/  # atomic-rename scratch dir (MUST be excluded from sync)
```

`upload/` may contain subdirectories; outputs preserve the relative path
(`upload/papers/foo.pdf` -> `processed/papers/foo.pdf`). The original always stays
in `upload/`. Removing a source from `upload/` removes its `processed/`/`failed/`
outputs on the next reconcile (see the readiness-marker note below).

## Crop configuration

The crop profile is layered, lowest precedence first:

1. **Built-in defaults**: `-p 10` (retain 10% of existing margins). Each page is
   cropped to its own bounding box (no `-u`/`-s`), so a page with wide margins is
   trimmed more than a page with narrow margins instead of all pages sharing one
   crop. Set `uniform`/`same_size` if you want a single consistent page box.
2. **Global `config.toml`** at the synced root, in a `[crop]` table. Editable from
   any device; on change the service re-crops everything that relied on defaults.
3. **Per-file sidecar** `<name>.pdf.toml` next to the source PDF in `upload/`.
   Highest precedence; absent keys fall back to the effective default profile.

Both `[crop]` and the sidecar use the same keys (each maps to a `pdfcropmargins`
flag):

```toml
percent_retain   = 15            # -p PCT          (single value)
percent_retain4  = [50,20,40,10] # -p4 L B R T     (overrides percent_retain)
uniform          = true          # -u
same_size        = true          # -s
absolute4        = [0,0,12,0]    # -a4 L B R T     (bp to crop; negative adds space)
pre_crop         = 5             # -ap BP          (pre-crop before bbox detect)
threshold        = 191           # -t BYTEVAL      (background detection threshold)
use_ghostscript  = true          # -gs            (ghostscript bbox detection)
pages            = "2-"          # -g PAGESTR      (restrict cropped pages)
password         = "secret"      # -pw PASSWD      (encrypted input)
strip_header_footer = true       # trim running header/footer (default false)
```

`strip_header_footer` is opt-in (default `false`) and is the one key that is not
a `pdfcropmargins` flag: it tells the helper to also detect and trim the running
header (conference/journal line, running title) and footer (page number) that
`pdfcropmargins` leaves in place because they are real ink, not whitespace. The
trim is part of the same lossless crop (CropBox/MediaBox only, no re-render) and
is conservative: it abstains and trims nothing whenever it is not confident, so a
paper with no running header, a title page, or a page with a figure at the top is
never damaged. Detection is text-agnostic (geometry, not text content), so
verso/recto alternation and changing page numbers do not defeat it.

Example global `config.toml`:

```toml
[crop]
percent_retain = 8
pre_crop       = 5
```

Unknown keys are rejected. A malformed `config.toml` does not crash the service:
it keeps the last-known-good profile and writes the reason to
`config.error.log` (visible from any device); the error file clears once a valid
config loads.

## Deploy via the NixOS module

Add this flake as an input and import its module:

```nix
{
  inputs.scribe-crop.url = "github:youruser/drive-pdf-margin-helper";

  outputs = {nixpkgs, scribe-crop, ...}: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        scribe-crop.nixosModules.default
        {
          services.scribe-crop = {
            enable = true;
            user = "alice";
            root = "/home/alice/OneDrive/ScribeCrop";
            readinessMarker = "/run/onedrive-ready/mirror-ready";
            settings = {
              stability_seconds = 5.0;
              process_timeout_seconds = 300.0;
              worker_count = 1;
              max_input_bytes = 268435456;
              retry_backoff = {
                initial_seconds = 30.0;
                max_seconds = 3600.0;
                multiplier = 2.0;
                max_attempts = 8;
              };
            };
          };
        }
      ];
    };
  };
}
```

The module runs `scribe-crop -c <generated-server.toml> run` as the configured
user/group, with `Restart=on-failure`, ordered `After=`/`Wants=` the onedrive
unit, and the `scribe-crop` package (which carries `pdfcropmargins` on its runtime
PATH) on the unit PATH. It provisions a systemd `StateDirectory` and
`RuntimeDirectory` named `scribe-crop`. See `nix/module.nix` for the full option
schema and descriptions.

### OneDrive client (configured separately)

This module does **not** own your OneDrive account config. Configure
`services.onedrive` (or run the abraunegg client yourself) for the same user, and:

- **Restrict `sync_list` to the `ScribeCrop/` subtree** so the client only mirrors
  that folder.
- **Exclude `ScribeCrop/.scribe-crop-tmp/` from the `sync_list`.** This is the
  atomic-rename scratch dir; it shares the root mount (so renames into
  `processed/`/`failed/` are atomic) but must never sync to the cloud.
- Run the client in monitor mode so the local mirror stays current both ways.

Example abraunegg `sync_list` (include the subtree, exclude the scratch dir):

```
ScribeCrop/
!ScribeCrop/.scribe-crop-tmp
```

The configured `user`/`group` must already exist and own the mirror subtree; the
module runs as that user but does not create it.

### Readiness marker (reverse-GC safety)

The reverse-reconcile GC removes outputs whose source has been deleted from
`upload/`. That is destructive, so it only runs when the mirror is known-current.
Two ways to signal that:

- `mirrorCurrent = true`: always assume current. Only safe if the mirror is
  guaranteed synced before the service starts.
- `readinessMarker = <path>`: the GC runs only while that path exists. Have your
  onedrive integration create the marker once an initial sync completes. **The
  marker must live OUTSIDE the synced subtree** (e.g. under `/run`, as in the
  example) so it never round-trips to the cloud. **Do not place it under
  `/run/scribe-crop`** (this service's own `RuntimeDirectory`): systemd wipes and
  recreates that directory empty on every stop/restart, and with
  `Restart=on-failure` restarts are routine, so a marker there would be silently
  deleted and reverse-GC disabled. Point it at a path owned by the sync-readiness
  signal instead (e.g. `/run/onedrive-ready/`).

If neither is set, the forward pass still runs (new/changed PDFs are cropped) but
deletions are never garbage-collected.

## Development

`uv` manages the Python project; system tools come from Nix.

```sh
nix develop            # python314 + uv + pdfcropmargins on PATH
uv run pytest          # unit + integration tests (no cloud needed)
uv run ruff check      # lint

nix build .#scribe-crop  # build the application
nix flake check          # evaluate package, module, and dev shell
```

Local testing needs no OneDrive: point `root` at a scratch directory containing
`upload/processed/failed` and drop files into `upload/`.
