from __future__ import annotations

import hashlib
import importlib.metadata
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ToolVersion:
    pdfcropmargins: str | None
    ghostscript: str | None

    def as_token(self) -> str:
        return f"pdfcropmargins={self.pdfcropmargins};ghostscript={self.ghostscript}"


def _probe(argv: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, check=False
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or proc.stderr).strip()
    return out.splitlines()[0].strip() if out else None


def _dist_version(name: str) -> str | None:
    # Fallback version source: the installed package version of the library the
    # crop shim imports. Used when the `pdfcropmargins` binary is not on PATH (the
    # sibling-shim deployment), so the recorded version still reflects what crops
    # rather than being None.
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def probe_tool_version() -> ToolVersion:
    # Prefer the `pdfcropmargins --version` CLI string when the binary is on PATH:
    # it matches what earlier versions recorded, so existing fingerprints are
    # preserved and unchanged files are not re-cropped on rollout. Fall back to the
    # installed package version only when the binary is absent from PATH (the
    # sibling-shim deployment), so the recorded version still tracks the library
    # the shim imports instead of being None.
    return ToolVersion(
        pdfcropmargins=_probe(["pdfcropmargins", "--version"])
        or _dist_version("pdfcropmargins"),
        ghostscript=_probe(["gs", "--version"]),
    )


_MISSING = b"\x00"


_CHUNK = 1 << 20


def _digest_part(label: str, data: bytes | None) -> bytes:
    h = hashlib.sha256()
    h.update(label.encode("utf-8"))
    if data is None:
        h.update(_MISSING)
    else:
        h.update(b"\x01")
        h.update(data)
    return h.digest()


def _digest_file(label: str, path: Path) -> bytes:
    h = hashlib.sha256()
    h.update(label.encode("utf-8"))
    h.update(b"\x01")
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.digest()


def compute_fingerprint(
    pdf_path: Path | str,
    *,
    pdf_bytes: bytes | None = None,
    profile_token: str,
    tool_version: ToolVersion,
    strip_token: str | None = None,
) -> str:
    # Key = these bytes + this exact argv (profile_token, already merged) + this
    # toolchain. strip_token is added only when stripping is enabled, so a
    # disabled file's key is byte-identical to a no-feature run and never re-crops.
    h = hashlib.sha256()
    if pdf_bytes is None:
        h.update(_digest_file("pdf", Path(pdf_path)))
    else:
        h.update(_digest_part("pdf", pdf_bytes))
    h.update(_digest_part("tool", tool_version.as_token().encode("utf-8")))
    h.update(_digest_part("profile", profile_token.encode("utf-8")))
    if strip_token is not None:
        h.update(_digest_part("strip", strip_token.encode("utf-8")))
    return h.hexdigest()


def compute_oversize_fingerprint(*, size: int) -> str:
    # Oversize rejection depends only on the byte count, not on PDF content, the
    # crop profile, or the toolchain: the file is refused before it is read. We
    # key it off the size (under a distinct domain tag so it never collides with
    # a normal fingerprint) so that raising max_input_bytes later routes the file
    # to the normal path, whose fingerprint cannot match this record, and it
    # reprocesses instead of staying suppressed forever.
    h = hashlib.sha256()
    h.update(_digest_part("oversize", str(size).encode("utf-8")))
    return h.hexdigest()
