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
from .crop_shim import (
    CONTENT_STDERR_PATTERNS as _CONTENT_STDERR_PATTERNS,
    STRIP_FLAG,
    _CONTENT_MARKER as _SHIM_CONTENT_MARKER,
    _ENV_MARKER as _SHIM_ENV_MARKER,
)
from .detector import DEFAULT_PARAMS
from .fingerprint import (
    ToolVersion,
    compute_fingerprint,
    compute_oversize_fingerprint,
)
from .profile import (
    BUILTIN_PROFILE,
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


def classify_run_failure(result: RunResult) -> ResultKind:
    if result.returncode == 0:
        return ResultKind.SUCCESS
    blob = result.stderr.lower()
    # Shim markers are authoritative: a detector/library traceback containing a
    # content word like "bounding box" must not be misread as a suppression.
    if _SHIM_ENV_MARKER.lower() in blob:
        return ResultKind.ENVIRONMENTAL_FAILURE
    if _SHIM_CONTENT_MARKER.lower() in blob:
        return ResultKind.CONTENT_FAILURE
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
    lines = []
    if argv is not None:
        lines.append("command: " + " ".join(argv))
    lines.append(reason.rstrip("\n"))
    content = "\n".join(lines) + "\n"
    # Idempotent: the malformed-input path re-checks the file every reconcile
    # pass, so skip the rewrite when the log is unchanged to avoid sync churn.
    try:
        if log_dest.read_text() == content:
            return
    except OSError:
        pass
    log_dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _mkstemp_in(tmp_dir)
    try:
        tmp.write_text(content)
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

    try:
        sidecar_bytes: bytes | None = sidecar.read_bytes()
    except FileNotFoundError:
        sidecar_bytes = None

    # Reject oversize inputs before hashing the PDF so a multi-GB file is never read.
    size = input_pdf.stat().st_size
    if size > config.max_input_bytes:
        fp = compute_oversize_fingerprint(size=size)
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

    # Resolve the effective profile before fingerprinting: the fingerprint keys
    # on the emitted argv, so the profile applied is tautologically the one
    # recorded. A malformed sidecar/profile has no argv to fingerprint and so is
    # not processed (no crop, no suppression) beyond a log in failed/.
    try:
        sidecar_data = (
            _parse_sidecar(sidecar_bytes) if sidecar_bytes is not None else None
        )
    except (ValueError, UnicodeDecodeError) as exc:
        return _record_malformed(
            relpath,
            failed_dest,
            processed_dest,
            reason=f"invalid sidecar: {exc}",
            store=store,
            tmp_dir=config.tmp_dir,
        )

    try:
        profile = merge_profiles(BUILTIN_PROFILE, drive_crop, sidecar_data)
    except (UnknownProfileKey, ValueError) as exc:
        return _record_malformed(
            relpath,
            failed_dest,
            processed_dest,
            reason=f"invalid profile: {exc}",
            store=store,
            tmp_dir=config.tmp_dir,
        )

    profile_argv = profile_to_argv(profile)
    # Folded into the fingerprint and command only when enabled, so a disabled
    # file keeps today's argv-only key and does not re-crop on rollout.
    strip = profile.strip_header_footer
    strip_token = DEFAULT_PARAMS.token() if strip else None
    pdf_bytes = input_pdf.read_bytes()
    fp = fingerprint_fn(
        input_pdf,
        pdf_bytes=pdf_bytes,
        profile_token=" ".join(profile_argv),
        tool_version=tool_version,
        strip_token=strip_token,
    )

    record = store.get(relpath)
    if record is not None and record.fingerprint == fp:
        if record.outcome is Outcome.SUCCESS and processed_dest.exists():
            return ProcessResult(relpath, ResultKind.SKIPPED, fingerprint=fp)
        if record.outcome is Outcome.CONTENT_FAILURE:
            return ProcessResult(relpath, ResultKind.SKIPPED, fingerprint=fp)

    temp_out = _mkstemp_in(config.tmp_dir)

    # `binary` is the crop shim (it wraps pdfcropmargins). When stripping is on
    # we pass our directive flag; otherwise the shim is a pass-through and the
    # remaining argv is exactly what a direct pdfcropmargins call would receive.
    strip_flag = [STRIP_FLAG] if strip else []
    argv = [binary, *strip_flag, *profile_argv, "-o", str(temp_out), str(input_pdf)]

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


def _record_malformed(
    relpath: str,
    failed_dest: Path,
    processed_dest: Path,
    *,
    reason: str,
    store: StateStore,
    tmp_dir: Path,
) -> ProcessResult:
    # A malformed sidecar/profile has no effective argv, so there is nothing to
    # fingerprint and nothing to crop. We only surface the reason as a .log in
    # failed/ (no PDF copy) so the operator sees it on any device, and clear any
    # stale outputs from when the file last parsed. The empty fingerprint is
    # recorded purely so reverse-GC tracks the relpath and removes the .log once
    # the source is deleted; it never matches a computed key, so the file is
    # re-checked (and fails fast, before any crop) on every reconcile pass.
    _write_failure_log(_failed_log_path(failed_dest), tmp_dir, argv=None, reason=reason)
    failed_dest.unlink(missing_ok=True)
    processed_dest.unlink(missing_ok=True)
    store.upsert(relpath, "", Outcome.CONTENT_FAILURE)
    return ProcessResult(
        relpath, ResultKind.CONTENT_FAILURE, fingerprint="", reason=reason
    )


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
