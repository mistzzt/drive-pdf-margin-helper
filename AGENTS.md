# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`scribe-crop` auto-crops PDF margins so they read well on a Kindle Scribe. It watches a
local directory tree (the `ScribeCrop/` subtree) that an external OneDrive client keeps in
sync, crops new/changed PDFs with `pdfcropmargins` (CropBox/MediaBox only, lossless, no
re-render), and writes results back into the same tree. It never talks to OneDrive directly:
the local mirror is its entire world.

`docs/design.md` is the authoritative spec for behavior, the sync-client contract, and the
rationale behind every decision. Read it before changing processing, reconcile, or the
sync boundary. `README.md` documents the user-facing config and NixOS deployment.

## Commands

System tools (`pdfcropmargins`, `ghostscript`) come from Nix; `uv` manages the Python side.

```sh
nix develop              # dev shell: python314 + uv + pdfcropmargins on PATH
uv run pytest            # full test suite (no cloud/OneDrive needed)
uv run pytest tests/test_processor.py::test_name   # single test
uv run ruff check        # lint (ruff config is defaults; no pyproject [tool.ruff])

nix build .#scribe-crop  # build the application
nix flake check          # evaluate package + NixOS module-eval + dev shell
```

Targets Python 3.14. Only third-party runtime dep is `watchdog`; `sqlite3` and `tomllib`
are stdlib. Tests inject fakes (no real `pdfcropmargins` binary required) via the `runner`,
`clock`, `sleep`, and `process_fn` seams.

Run locally with no cloud: point `root` at a scratch dir containing `upload/processed/failed`,
then `scribe-crop -c <server.toml> run` (or `reconcile` for a one-shot pass).

## Architecture

Data flows through a single-source-of-truth pipeline keyed on the source PDF's relative path
under `upload/`. Output paths mirror that relpath into `processed/` and `failed/`.

- **`profile.py`** is the one place that maps crop config keys to `pdfcropmargins` flags
  (`FLAG_MAP`). Profiles layer built-in < drive `config.toml [crop]` < per-file `.pdf.toml`
  sidecar (`merge_profiles`). All config validation/coercion goes through `validate_and_coerce`.
  Adding a crop knob means editing `FLAG_MAP` and `CropProfile` here only.
- **`fingerprint.py`** computes the dedup key: `hash(pdf + sidecar + drive config + tool
  version + profile_token)`, where `profile_token` is a digest of the built-in default
  profile (`BUILTIN_PROFILE_TOKEN` in `profile.py`). Any change to inputs, the toolchain,
  the drive config, or the built-in defaults invalidates the key and forces a re-crop, so
  editing built-in defaults needs no manual version bump. Oversize inputs use a separate
  size-keyed fingerprint so raising `max_input_bytes` later un-suppresses them.
- **`processor.py`** (`process_pdf`) is the core unit: single consistent read of all inputs,
  fingerprint skip-check against the state store, build argv, run with timeout, then publish.
  Failures are classified into `CONTENT_FAILURE` (input-bound, suppressed permanently for
  identical bytes, copied to `failed/` with a `.log`) vs `ENVIRONMENTAL_FAILURE` (timeout /
  OOM / missing binary, retried with backoff, never suppressed). Classification of nonzero
  exits is by stderr substring (`_CONTENT_STDERR_PATTERNS`); unrecognized = environmental.
- **`state.py`** is a WAL-mode SQLite store (relpath -> fingerprint + outcome + timestamps),
  thread-safe via a lock, shared between the main and worker threads.
- **`reconcile.py`** does the backstop passes: forward (process anything whose fingerprint
  differs) and reverse GC (delete outputs + state for sources confirmed deleted). The watcher
  handles steady state; reconcile catches missed events and deletions.
- **`watcher.py`** wraps `watchdog`/inotify. It routes `*.pdf`/`*.pdf.toml` events under
  `upload/` (a `.toml` event maps back to its PDF) and root `config.toml` events into a queue,
  with per-path debouncing.
- **`service.py`** (`Service`) wires it together: loads config, runs startup reconcile, spawns
  a worker queue + heartbeat, owns the stability check and retry/backoff loop, and reloads the
  drive config on change. `MirrorReadiness` gates the destructive reverse-GC.
- **`cli.py`** resolves the binary, parses args, and runs `run` or `reconcile`.

## Critical invariants (do not break)

- **Sole writer of `processed/`/`failed/`.** Cloud-origin changes under those dirs are never
  treated as inputs. Only `upload/` events and root `config.toml` trigger work.
- **Atomic publish only.** Outputs are written to `<root>/.scribe-crop-tmp/` and `os.replace`d
  into place, so the sync client never sees a partial file (which would create `name-1.pdf`
  conflict copies). `rename(2)` is atomic only within one mount, so the temp dir MUST share the
  root mount and stay excluded from sync.
- **Publish-then-record ordering.** Publish the output, then record the fingerprint. A crash
  between the two is safe: the next reconcile recomputes the same fingerprint, finds the output,
  and re-records.
- **Reverse-GC is destructive and gated.** It runs only when the mirror is known-current
  (`assume_current` or an existing `readiness_marker` outside the synced subtree). An absent
  source on a lagging/unsynced mirror is NOT a deletion. The forward pass has no such hazard.
- **Single consistent read.** Read each input's bytes once and feed the same bytes to both the
  fingerprint and the profile/parser, so the profile applied always matches the fingerprint
  recorded, even if a config reload races.

## Project conventions

- No migration or backward-compatibility code unless explicitly requested.
- `from __future__ import annotations` at the top of every module; dataclasses are `frozen=True`
  where practical.
- Behavior is driven by injectable seams (runner, clock, sleep, fingerprint_fn) so tests stay
  cloud-free and deterministic; preserve those seams when extending.

## Nix packaging

`flake.nix` builds the app and exposes `nixosModules.default`. `nix/pdfcropmargins.nix` vendors
the crop tool. `nix/module.nix` is the `services.scribe-crop` NixOS module (generates a
`server.toml`, runs `scribe-crop ... run` as the configured user, provisions State/Runtime
dirs, orders after the onedrive unit). `ghostscript` is added to the unit PATH explicitly
because the service probes `gs --version` itself for the fingerprint. The OneDrive client
(account, auth, `sync_list`) is configured separately and is not owned by this module.
