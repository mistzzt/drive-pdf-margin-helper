from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum


class FlagKind(Enum):
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    QUAD = "quad"


@dataclass(frozen=True)
class FlagSpec:
    flag: str
    kind: FlagKind


# Single source of truth: profile key -> pdfcropmargins CLI flag.
FLAG_MAP: dict[str, FlagSpec] = {
    "percent_retain": FlagSpec("-p", FlagKind.FLOAT),
    "percent_retain4": FlagSpec("-p4", FlagKind.QUAD),
    "uniform": FlagSpec("-u", FlagKind.BOOL),
    "same_size": FlagSpec("-s", FlagKind.BOOL),
    "absolute4": FlagSpec("-a4", FlagKind.QUAD),
    "pre_crop": FlagSpec("-ap", FlagKind.FLOAT),
    "threshold": FlagSpec("-t", FlagKind.INT),
    "use_ghostscript": FlagSpec("-gs", FlagKind.BOOL),
    "pages": FlagSpec("-g", FlagKind.STR),
    "password": FlagSpec("-pw", FlagKind.STR),
}

# Keys validated/merged like flags but not emitted to argv; directives for the shim.
SHIM_DIRECTIVE_MAP: dict[str, FlagKind] = {
    "strip_header_footer": FlagKind.BOOL,
}

# Every recognized profile key, flag or directive. One validate/coerce/merge
# path covers both; only FLAG_MAP keys reach profile_to_argv.
PROFILE_KEYS = frozenset(FLAG_MAP) | frozenset(SHIM_DIRECTIVE_MAP)


def _kind_of(key: str) -> FlagKind:
    if key in FLAG_MAP:
        return FLAG_MAP[key].kind
    return SHIM_DIRECTIVE_MAP[key]


class UnknownProfileKey(ValueError):
    pass


@dataclass(frozen=True)
class CropProfile:
    percent_retain: float | None = None
    percent_retain4: tuple[float, float, float, float] | None = None
    uniform: bool = False
    same_size: bool = False
    absolute4: tuple[float, float, float, float] | None = None
    pre_crop: float | None = None
    threshold: int | None = None
    use_ghostscript: bool = False
    pages: str | None = None
    password: str | None = None
    # Shim directive (not a pdfcropmargins flag).
    strip_header_footer: bool = False

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> CropProfile:
        return cls(**validate_and_coerce(data))

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if _kind_of(f.name) is FlagKind.BOOL:
                if value:
                    result[f.name] = True
            elif value is not None:
                result[f.name] = value
        return result


def _is_number(value: object) -> bool:
    # bool is an int subclass; reject it for numeric fields.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce(key: str, value: object) -> object:
    kind = _kind_of(key)
    if kind is FlagKind.QUAD:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError(f"{key} must be a list of 4 numbers (L B R T)")
        if not all(_is_number(v) for v in value):
            raise ValueError(f"{key} elements must be numbers (L B R T)")
        return tuple(value)
    if kind is FlagKind.BOOL:
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        return value
    if kind is FlagKind.INT:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        return value
    if kind is FlagKind.FLOAT:
        if not _is_number(value):
            raise ValueError(f"{key} must be a number")
        return value
    if kind is FlagKind.STR:
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return value
    raise AssertionError(f"unhandled flag kind: {kind}")


def validate_and_coerce(data: dict[str, object]) -> dict[str, object]:
    unknown = set(data) - PROFILE_KEYS
    if unknown:
        raise UnknownProfileKey(
            f"unknown crop profile keys: {', '.join(sorted(unknown))}"
        )
    return {key: _coerce(key, value) for key, value in data.items()}


# Keys that cannot coexist; the last present wins (percent_retain4 over percent_retain).
_EXCLUSIVE_GROUPS: tuple[tuple[str, ...], ...] = (("percent_retain", "percent_retain4"),)


def _resolve_exclusive(effective: dict[str, object]) -> None:
    for group in _EXCLUSIVE_GROUPS:
        present = [key for key in group if key in effective]
        for key in present[:-1]:
            effective.pop(key)


# built-in < drive-config < sidecar
def merge_profiles(
    builtin: CropProfile,
    drive_config: dict[str, object] | None = None,
    sidecar: dict[str, object] | None = None,
) -> CropProfile:
    effective = builtin.to_dict()
    for layer in (drive_config, sidecar):
        if not layer:
            continue
        coerced = validate_and_coerce(layer)
        for group in _EXCLUSIVE_GROUPS:
            if any(key in coerced for key in group):
                for key in group:
                    effective.pop(key, None)
        effective.update(coerced)
    _resolve_exclusive(effective)
    return CropProfile(**effective)


def profile_to_argv(profile: CropProfile) -> list[str]:
    argv: list[str] = []
    effective = profile.to_dict()
    _resolve_exclusive(effective)
    for key in FLAG_MAP:
        if key not in effective:
            continue
        spec = FLAG_MAP[key]
        value = effective[key]
        if spec.kind is FlagKind.BOOL:
            argv.append(spec.flag)
        elif spec.kind is FlagKind.QUAD:
            argv.append(spec.flag)
            argv.extend(str(_fmt(v)) for v in value)  # type: ignore[union-attr]
        else:
            argv.append(spec.flag)
            argv.append(str(_fmt(value)))
    return argv


def _fmt(value: object) -> object:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


BUILTIN_PROFILE = CropProfile(uniform=False, same_size=False, percent_retain=10)
