import pytest

from scribe_crop.profile import (
    BUILTIN_PROFILE,
    CropProfile,
    UnknownProfileKey,
    merge_profiles,
    profile_to_argv,
)


def test_builtin_argv():
    argv = profile_to_argv(BUILTIN_PROFILE)
    assert argv == ["-p", "10"]


def test_all_flag_mappings():
    profile = CropProfile.from_mapping(
        {
            "percent_retain4": [50, 20, 40, 10],
            "uniform": True,
            "same_size": True,
            "absolute4": [0, 0, 12, 0],
            "pre_crop": 5,
            "threshold": 191,
            "use_ghostscript": True,
            "pages": "2-",
            "password": "secret",
        }
    )
    argv = profile_to_argv(profile)
    assert argv == [
        "-p4", "50", "20", "40", "10",
        "-u",
        "-s",
        "-a4", "0", "0", "12", "0",
        "-ap", "5",
        "-t", "191",
        "-gs",
        "-g", "2-",
        "-pw", "secret",
    ]


def test_percent_retain4_supersedes_percent_retain():
    profile = CropProfile.from_mapping(
        {"percent_retain": 10, "percent_retain4": [1, 2, 3, 4]}
    )
    argv = profile_to_argv(profile)
    assert "-p" not in argv
    assert argv[:5] == ["-p4", "1", "2", "3", "4"]


def test_sidecar_percent_retain_overrides_drive_percent_retain4():
    merged = merge_profiles(
        BUILTIN_PROFILE,
        drive_config={"percent_retain4": [1, 2, 3, 4]},
        sidecar={"percent_retain": 25},
    )
    argv = profile_to_argv(merged)
    assert "-p4" not in argv
    assert merged.percent_retain4 is None
    assert argv[argv.index("-p") + 1] == "25"


def test_sidecar_percent_retain4_overrides_drive_percent_retain():
    merged = merge_profiles(
        BUILTIN_PROFILE,
        drive_config={"percent_retain": 8},
        sidecar={"percent_retain4": [1, 2, 3, 4]},
    )
    argv = profile_to_argv(merged)
    assert "-p" not in argv
    assert merged.percent_retain is None
    assert argv[:5] == ["-p4", "1", "2", "3", "4"]


def test_within_layer_percent_retain4_wins_regardless_of_key_order():
    profile = CropProfile.from_mapping(
        {"percent_retain4": [1, 2, 3, 4], "percent_retain": 10}
    )
    argv = profile_to_argv(profile)
    assert "-p" not in argv
    assert argv[:5] == ["-p4", "1", "2", "3", "4"]


def test_false_bool_omits_flag():
    profile = CropProfile.from_mapping({"uniform": False, "percent_retain": 8})
    assert "-u" not in profile_to_argv(profile)


def test_unknown_key_rejected():
    with pytest.raises(UnknownProfileKey):
        CropProfile.from_mapping({"bogus": 1})


def test_merge_precedence_builtin_drive_sidecar():
    merged = merge_profiles(
        BUILTIN_PROFILE,
        drive_config={"percent_retain": 8, "pre_crop": 5},
        sidecar={"percent_retain": 15},
    )
    assert merged.percent_retain == 15  # sidecar wins
    assert merged.pre_crop == 5  # drive layer survives
    assert merged.uniform is False  # builtin survives
    assert merged.same_size is False


def test_merge_drive_over_builtin():
    merged = merge_profiles(BUILTIN_PROFILE, drive_config={"percent_retain": 8})
    assert merged.percent_retain == 8
    assert merged.uniform is False


def test_merge_rejects_unknown_in_layer():
    with pytest.raises(UnknownProfileKey):
        merge_profiles(BUILTIN_PROFILE, sidecar={"nope": True})


def test_merge_empty_layers_returns_builtin_equivalent():
    merged = merge_profiles(BUILTIN_PROFILE, None, None)
    assert profile_to_argv(merged) == profile_to_argv(BUILTIN_PROFILE)


def test_quad_must_have_four():
    with pytest.raises(ValueError):
        CropProfile.from_mapping({"absolute4": [1, 2, 3]})


def test_bool_must_be_bool():
    with pytest.raises(ValueError):
        CropProfile.from_mapping({"uniform": "yes"})


def test_strip_header_footer_not_emitted_as_flag():
    profile = CropProfile.from_mapping(
        {"strip_header_footer": True, "percent_retain": 8}
    )
    argv = profile_to_argv(profile)
    # The directive never reaches the pdfcropmargins argv.
    assert "strip_header_footer" not in " ".join(argv)
    assert "--strip-header-footer" not in argv
    assert argv == ["-p", "8"]
    assert profile.strip_header_footer is True


def test_strip_header_footer_defaults_off():
    assert BUILTIN_PROFILE.strip_header_footer is False


def test_strip_header_footer_merges_with_precedence():
    merged = merge_profiles(
        BUILTIN_PROFILE,
        drive_config={"strip_header_footer": True},
        sidecar={"strip_header_footer": False},
    )
    assert merged.strip_header_footer is False  # sidecar wins


def test_strip_header_footer_must_be_bool():
    with pytest.raises(ValueError):
        CropProfile.from_mapping({"strip_header_footer": "yes"})
