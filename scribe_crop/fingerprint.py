from __future__ import annotations

import hashlib
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


def probe_tool_version() -> ToolVersion:
    return ToolVersion(
        pdfcropmargins=_probe(["pdfcropmargins", "--version"]),
        ghostscript=_probe(["gs", "--version"]),
    )


_MISSING = b"\x00"


def _digest_part(label: str, data: bytes | None) -> bytes:
    h = hashlib.sha256()
    h.update(label.encode("utf-8"))
    if data is None:
        h.update(_MISSING)
    else:
        h.update(b"\x01")
        h.update(data)
    return h.digest()


def compute_fingerprint(
    pdf_path: Path | str,
    *,
    sidecar_path: Path | str | None,
    drive_config_bytes: bytes,
    tool_version: ToolVersion,
    profile_version: int,
) -> str:
    pdf_bytes = Path(pdf_path).read_bytes()
    sidecar_bytes: bytes | None = None
    if sidecar_path is not None:
        try:
            sidecar_bytes = Path(sidecar_path).read_bytes()
        except FileNotFoundError:
            sidecar_bytes = None

    h = hashlib.sha256()
    h.update(_digest_part("pdf", pdf_bytes))
    h.update(_digest_part("sidecar", sidecar_bytes))
    h.update(_digest_part("config", drive_config_bytes))
    h.update(_digest_part("tool", tool_version.as_token().encode("utf-8")))
    h.update(_digest_part("profile_version", str(profile_version).encode("utf-8")))
    return h.hexdigest()
