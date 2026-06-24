from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .profile import UnknownProfileKey, validate_and_coerce


@dataclass(frozen=True)
class RetryBackoff:
    initial_seconds: float = 30.0
    max_seconds: float = 3600.0
    multiplier: float = 2.0


@dataclass(frozen=True)
class ServerConfig:
    root: Path
    profile_version: int = 1
    stability_seconds: float = 5.0
    process_timeout_seconds: float = 300.0
    worker_count: int = 1
    max_input_bytes: int = 256 * 1024 * 1024
    retry_backoff: RetryBackoff = field(default_factory=RetryBackoff)
    state_path: Path | None = None

    @property
    def upload_dir(self) -> Path:
        return self.root / "upload"

    @property
    def processed_dir(self) -> Path:
        return self.root / "processed"

    @property
    def failed_dir(self) -> Path:
        return self.root / "failed"

    @property
    def drive_config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def config_error_path(self) -> Path:
        return self.root / "config.error.log"

    @property
    def resolved_state_path(self) -> Path:
        return self.state_path if self.state_path is not None else self.root / "state.db"


_SCALAR_FIELDS = {
    "profile_version": int,
    "stability_seconds": float,
    "process_timeout_seconds": float,
    "worker_count": int,
    "max_input_bytes": int,
}


_RETRY_BACKOFF_FIELDS = {"initial_seconds", "max_seconds", "multiplier"}
_ALLOWED_SERVER_KEYS = set(_SCALAR_FIELDS) | {"root", "state_path", "retry_backoff"}


def load_server_config(path: Path | str) -> ServerConfig:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    unknown = set(raw) - _ALLOWED_SERVER_KEYS
    if unknown:
        raise ValueError(f"unknown server config keys: {', '.join(sorted(unknown))}")

    if "root" not in raw:
        raise ValueError("server config requires 'root'")
    root = Path(raw["root"]).expanduser()

    kwargs: dict[str, object] = {"root": root}
    for key, conv in _SCALAR_FIELDS.items():
        if key in raw:
            kwargs[key] = conv(raw[key])
    if "state_path" in raw:
        kwargs["state_path"] = Path(raw["state_path"]).expanduser()
    if "retry_backoff" in raw:
        rb = raw["retry_backoff"]
        if not isinstance(rb, dict):
            raise ValueError("retry_backoff must be a table")
        unknown_rb = set(rb) - _RETRY_BACKOFF_FIELDS
        if unknown_rb:
            raise ValueError(
                f"unknown retry_backoff keys: {', '.join(sorted(unknown_rb))}"
            )
        kwargs["retry_backoff"] = RetryBackoff(
            initial_seconds=float(rb.get("initial_seconds", 30.0)),
            max_seconds=float(rb.get("max_seconds", 3600.0)),
            multiplier=float(rb.get("multiplier", 2.0)),
        )

    cfg = ServerConfig(**kwargs)  # type: ignore[arg-type]
    if cfg.worker_count < 1:
        raise ValueError("worker_count must be >= 1")
    for name in ("stability_seconds", "process_timeout_seconds", "max_input_bytes"):
        if getattr(cfg, name) < 0:
            raise ValueError(f"{name} must be non-negative")
    return cfg


@dataclass(frozen=True)
class DriveConfigResult:
    crop: dict[str, object] | None
    error: str | None
    raw_bytes: bytes | None

    @property
    def ok(self) -> bool:
        return self.error is None


def load_drive_config(path: Path | str) -> DriveConfigResult:
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        # Absent config is not an error: built-in defaults apply.
        return DriveConfigResult(crop={}, error=None, raw_bytes=b"")

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return DriveConfigResult(crop=None, error=f"parse error: {exc}", raw_bytes=raw_bytes)

    crop = data.get("crop", {})
    if not isinstance(crop, dict):
        return DriveConfigResult(
            crop=None, error="[crop] must be a table", raw_bytes=raw_bytes
        )

    try:
        validated = validate_and_coerce(crop)
    except (ValueError, UnknownProfileKey) as exc:
        return DriveConfigResult(crop=None, error=str(exc), raw_bytes=raw_bytes)

    return DriveConfigResult(crop=validated, error=None, raw_bytes=raw_bytes)
