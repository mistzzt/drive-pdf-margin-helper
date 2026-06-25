from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import ServerConfig
from .fingerprint import (
    ToolVersion,
    compute_fingerprint,
    compute_oversize_fingerprint,
)
from .profile import (
    BUILTIN_PROFILE,
    BUILTIN_PROFILE_TOKEN,
    UnknownProfileKey,
    merge_profiles,
    profile_to_argv,
)
from .state import Outcome, StateStore


class ResultKind(Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    CONTENT_FAILURE = "content_failure"
    ENVIRONMENTAL_FAILURE = "environmental_failure"
    CANCELLED = "cancelled"

    @property
    def retryable(self) -> bool:
        return self is ResultKind.ENVIRONMENTAL_FAILURE


@dataclass(frozen=True)
class ProcessResult:
    relpath: str
    kind: ResultKind
    fingerprint: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


# A runner returns RunResult on a completed process, or raises one of the
# exceptions below. Injected so tests need no real binary.
Runner = Callable[[list[str], float], RunResult]


class BinaryNotFound(Exception):
    pass


class RunTimeout(Exception):
    pass


class OutOfMemory(Exception):
    pass


def subprocess_runner(argv: list[str], timeout: float) -> RunResult:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise BinaryNotFound(str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise RunTimeout(str(exc)) from exc
    # A process killed by SIGKILL (often the OOM killer) reports -9.
    if proc.returncode == -9:
        raise OutOfMemory("process killed (signal 9)")
    return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# stderr substrings that mark a content (input-bound) failure: the same bytes
# will never succeed without the user changing the input or its sidecar.
_CONTENT_STDERR_PATTERNS = (
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


def classify_run_failure(result: RunResult) -> ResultKind:
    if result.returncode == 0:
        return ResultKind.SUCCESS
    blob = result.stderr.lower()
    if any(pat in blob for pat in _CONTENT_STDERR_PATTERNS):
        return ResultKind.CONTENT_FAILURE
    # Unrecognized nonzero exit is environmental (retryable) per the design.
    return ResultKind.ENVIRONMENTAL_FAILURE


def _sidecar_path(pdf: Path) -> Path:
    return pdf.with_name(pdf.name + ".toml")


def _parse_sidecar(raw: bytes) -> dict[str, object]:
    return tomllib.loads(raw.decode("utf-8"))


def _mkstemp_in(tmp_dir: Path) -> Path:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=tmp_dir, suffix=".tmp")
    os.close(fd)
    return Path(name)


def _atomic_publish(src_temp: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src_temp, dest)


def _publish_original_to_failed(original: Path, dest: Path, tmp_dir: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _mkstemp_in(tmp_dir)
    try:
        shutil.copyfile(original, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _write_failure_log(
    log_dest: Path, tmp_dir: Path, *, argv: list[str] | None, reason: str
) -> None:
    log_dest.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if argv is not None:
        lines.append("command: " + " ".join(argv))
    lines.append(reason.rstrip("\n"))
    tmp = _mkstemp_in(tmp_dir)
    try:
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, log_dest)
    finally:
        tmp.unlink(missing_ok=True)


def _clear_failed(failed_pdf: Path) -> None:
    failed_pdf.unlink(missing_ok=True)
    _failed_log_path(failed_pdf).unlink(missing_ok=True)


def _failed_log_path(failed_pdf: Path) -> Path:
    return failed_pdf.with_name(failed_pdf.name + ".log")


def process_pdf(
    relpath: str,
    *,
    config: ServerConfig,
    drive_crop: dict[str, object],
    drive_config_bytes: bytes,
    store: StateStore,
    tool_version: ToolVersion,
    binary: str,
    runner: Runner = subprocess_runner,
    fingerprint_fn: Callable[..., str] = compute_fingerprint,
) -> ProcessResult:
    rel = Path(relpath)
    input_pdf = config.upload_dir / rel
    sidecar = _sidecar_path(input_pdf)
    processed_dest = config.processed_dir / rel
    failed_dest = config.failed_dir / rel

    # Single consistent read: read the sidecar bytes once and feed the same
    # bytes to both the fingerprint and the profile parser, so the profile we
    # apply always matches the fingerprint we record.
    try:
        sidecar_bytes: bytes | None = sidecar.read_bytes()
    except FileNotFoundError:
        sidecar_bytes = None

    # Reject oversize inputs before hashing the PDF so a multi-GB file is never read.
    size = input_pdf.stat().st_size
    if size > config.max_input_bytes:
        fp = compute_oversize_fingerprint(
            size=size,
            sidecar_bytes=sidecar_bytes,
            drive_config_bytes=drive_config_bytes,
            tool_version=tool_version,
            profile_token=BUILTIN_PROFILE_TOKEN,
        )
        record = store.get(relpath)
        if (
            record is not None
            and record.fingerprint == fp
            and record.outcome is Outcome.CONTENT_FAILURE
            and failed_dest.exists()
        ):
            return ProcessResult(relpath, ResultKind.SKIPPED, fingerprint=fp)
        reason = (
            f"input is {size} bytes, exceeds max_input_bytes "
            f"({config.max_input_bytes})"
        )
        return _record_content_failure(
            relpath,
            input_pdf,
            failed_dest,
            processed_dest,
            argv=None,
            reason=reason,
            fp=fp,
            store=store,
            tmp_dir=config.tmp_dir,
        )

    pdf_bytes = input_pdf.read_bytes()
    fp = fingerprint_fn(
        input_pdf,
        pdf_bytes=pdf_bytes,
        sidecar_bytes=sidecar_bytes,
        drive_config_bytes=drive_config_bytes,
        tool_version=tool_version,
        profile_token=BUILTIN_PROFILE_TOKEN,
    )

    record = store.get(relpath)
    if record is not None and record.fingerprint == fp:
        if record.outcome is Outcome.SUCCESS and processed_dest.exists():
            return ProcessResult(relpath, ResultKind.SKIPPED, fingerprint=fp)
        if record.outcome is Outcome.CONTENT_FAILURE:
            return ProcessResult(relpath, ResultKind.SKIPPED, fingerprint=fp)

    try:
        sidecar_data = (
            _parse_sidecar(sidecar_bytes) if sidecar_bytes is not None else None
        )
    except (ValueError, UnicodeDecodeError) as exc:
        return _record_content_failure(
            relpath,
            input_pdf,
            failed_dest,
            processed_dest,
            argv=None,
            reason=f"invalid sidecar: {exc}",
            fp=fp,
            store=store,
            tmp_dir=config.tmp_dir,
        )

    try:
        profile = merge_profiles(BUILTIN_PROFILE, drive_crop, sidecar_data)
    except (UnknownProfileKey, ValueError) as exc:
        return _record_content_failure(
            relpath,
            input_pdf,
            failed_dest,
            processed_dest,
            argv=None,
            reason=f"invalid profile: {exc}",
            fp=fp,
            store=store,
            tmp_dir=config.tmp_dir,
        )

    temp_out = _mkstemp_in(config.tmp_dir)

    argv = [binary, *profile_to_argv(profile), "-o", str(temp_out), str(input_pdf)]

    try:
        try:
            run = runner(argv, config.process_timeout_seconds)
        except BinaryNotFound as exc:
            return ProcessResult(
                relpath,
                ResultKind.ENVIRONMENTAL_FAILURE,
                reason=f"binary not found: {exc}",
            )
        except RunTimeout as exc:
            return ProcessResult(
                relpath,
                ResultKind.ENVIRONMENTAL_FAILURE,
                reason=f"timeout after {config.process_timeout_seconds}s: {exc}",
            )
        except OutOfMemory as exc:
            return ProcessResult(
                relpath,
                ResultKind.ENVIRONMENTAL_FAILURE,
                reason=f"out of memory: {exc}",
            )

        kind = classify_run_failure(run)
        if kind is ResultKind.SUCCESS:
            _atomic_publish(temp_out, processed_dest)
            store.upsert(relpath, fp, Outcome.SUCCESS)
            _clear_failed(failed_dest)
            return ProcessResult(relpath, ResultKind.SUCCESS, fingerprint=fp)

        if kind is ResultKind.ENVIRONMENTAL_FAILURE:
            return ProcessResult(
                relpath,
                ResultKind.ENVIRONMENTAL_FAILURE,
                reason=f"exit {run.returncode}: {run.stderr.strip()}",
            )

        return _record_content_failure(
            relpath,
            input_pdf,
            failed_dest,
            processed_dest,
            argv=argv,
            reason=f"exit {run.returncode}: {run.stderr.strip()}",
            fp=fp,
            store=store,
            tmp_dir=config.tmp_dir,
        )
    finally:
        temp_out.unlink(missing_ok=True)


def _record_content_failure(
    relpath: str,
    original: Path,
    failed_dest: Path,
    processed_dest: Path,
    *,
    argv: list[str] | None,
    reason: str,
    fp: str,
    store: StateStore,
    tmp_dir: Path,
) -> ProcessResult:
    _publish_original_to_failed(original, failed_dest, tmp_dir)
    _write_failure_log(_failed_log_path(failed_dest), tmp_dir, argv=argv, reason=reason)
    processed_dest.unlink(missing_ok=True)
    store.upsert(relpath, fp, Outcome.CONTENT_FAILURE)
    return ProcessResult(
        relpath, ResultKind.CONTENT_FAILURE, fingerprint=fp, reason=reason
    )
