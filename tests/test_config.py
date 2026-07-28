from pathlib import Path

import pytest

from scribe_crop.config import load_drive_config, load_server_config


def test_server_config_defaults(tmp_path):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text('root = "/srv/ScribeCrop"\n')
    cfg = load_server_config(cfg_path)
    assert cfg.root == Path("/srv/ScribeCrop")
    assert cfg.worker_count == 1
    assert cfg.stability_seconds == 5.0
    assert cfg.upload_dir == Path("/srv/ScribeCrop/upload")
    assert cfg.processed_dir == Path("/srv/ScribeCrop/processed")
    assert cfg.failed_dir == Path("/srv/ScribeCrop/failed")
    assert cfg.resolved_state_path == Path("/srv/ScribeCrop/state.db")


def test_server_config_overrides(tmp_path):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text(
        'root = "/srv/x"\n'
        "worker_count = 3\n"
        "max_input_bytes = 1024\n"
        'state_path = "/var/state.db"\n'
        "[retry_backoff]\n"
        "initial_seconds = 10\n"
        "max_seconds = 100\n"
        "multiplier = 3\n"
    )
    cfg = load_server_config(cfg_path)
    assert cfg.worker_count == 3
    assert cfg.max_input_bytes == 1024
    assert cfg.resolved_state_path == Path("/var/state.db")
    assert cfg.retry_backoff.initial_seconds == 10.0
    assert cfg.retry_backoff.multiplier == 3.0


@pytest.mark.parametrize(
    "backoff",
    [
        "initial_seconds = 0\nmax_seconds = 100\nmultiplier = 2\n",
        "initial_seconds = -1\nmax_seconds = 100\nmultiplier = 2\n",
        "initial_seconds = 10\nmax_seconds = 100\nmultiplier = 0.5\n",
        "initial_seconds = 10\nmax_seconds = 5\nmultiplier = 2\n",
    ],
)
def test_server_config_rejects_bad_retry_backoff(tmp_path, backoff):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text('root = "/srv/x"\n[retry_backoff]\n' + backoff)
    with pytest.raises(ValueError):
        load_server_config(cfg_path)


def test_drive_config_valid(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[crop]\npercent_retain = 8\nfit_scope = "page"\npre_crop = 5\n')
    result = load_drive_config(p)
    assert result.ok
    assert result.error is None
    assert result.crop == {"percent_retain": 8, "fit_scope": "page", "pre_crop": 5}


def test_drive_config_absent_is_empty_not_error(tmp_path):
    result = load_drive_config(tmp_path / "missing.toml")
    assert result.ok
    assert result.crop == {}


def test_drive_config_parse_error_falls_back(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[crop\npercent_retain = ")
    result = load_drive_config(p)
    assert not result.ok
    assert result.crop is None
    assert "parse error" in result.error


def test_drive_config_unknown_key_falls_back(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[crop]\nbogus = 1\n")
    result = load_drive_config(p)
    assert not result.ok
    assert result.crop is None
    assert "bogus" in result.error


def test_drive_config_bad_type_falls_back(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[crop]\nfit_reader = "yes"\n')
    result = load_drive_config(p)
    assert not result.ok
    assert result.crop is None


@pytest.mark.parametrize("key", ["uniform", "same_size"])
def test_drive_config_rejects_removed_keys(tmp_path, key):
    # Removed from the schema with reader-fit; a live config still using one
    # must fail validation (the service then falls back with a config.error.log).
    p = tmp_path / "config.toml"
    p.write_text(f"[crop]\n{key} = true\n")
    result = load_drive_config(p)
    assert not result.ok
    assert key in result.error


def test_drive_config_crop_not_table(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("crop = 5\n")
    result = load_drive_config(p)
    assert not result.ok
    assert "table" in result.error


def test_reader_defaults_to_scribe_colorsoft(tmp_path):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text('root = "/srv/x"\n')
    cfg = load_server_config(cfg_path)
    assert cfg.reader.screen_width_in == 6.6
    assert cfg.reader.screen_height_in == 8.8
    # 1 in = 72 pt exactly, so nothing is lost converting the spec-sheet inches.
    assert cfg.reader.screen_width_pt == 6.6 * 72
    assert cfg.reader.screen_height_pt == 8.8 * 72


def test_reader_device_preset(tmp_path):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text('root = "/srv/x"\n[reader]\ndevice = "scribe-colorsoft"\n')
    cfg = load_server_config(cfg_path)
    assert cfg.reader.screen_width_in == 6.6


def test_reader_explicit_dimensions(tmp_path):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text(
        'root = "/srv/x"\n[reader]\nscreen_width_in = 5.0\nscreen_height_in = 7.0\n'
    )
    cfg = load_server_config(cfg_path)
    assert cfg.reader.screen_width_in == 5.0
    assert cfg.reader.screen_height_pt == 504.0


@pytest.mark.parametrize(
    "body",
    [
        # device conflicts with explicit dimensions
        'device = "scribe-colorsoft"\nscreen_width_in = 5.0\nscreen_height_in = 7.0\n',
        'device = "scribe-colorsoft"\nscreen_width_in = 5.0\n',
        # dimensions must come as a pair
        "screen_width_in = 5.0\n",
        "screen_height_in = 7.0\n",
        # positive
        "screen_width_in = 0\nscreen_height_in = 7.0\n",
        "screen_width_in = 5.0\nscreen_height_in = -1\n",
        # types and unknown keys
        'screen_width_in = "5"\nscreen_height_in = 7.0\n',
        "ppi = 300\n",
        'device = "kindle-oasis"\n',
    ],
)
def test_reader_validation_rejects(tmp_path, body):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text('root = "/srv/x"\n[reader]\n' + body)
    with pytest.raises(ValueError):
        load_server_config(cfg_path)


def test_reader_must_be_table(tmp_path):
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text('root = "/srv/x"\nreader = 5\n')
    with pytest.raises(ValueError):
        load_server_config(cfg_path)


def test_reader_is_a_known_server_key(tmp_path):
    # Regression: [reader] must be in _ALLOWED_SERVER_KEYS or the whole file is
    # rejected as an unknown key before _parse_reader is ever reached.
    cfg_path = tmp_path / "server.toml"
    cfg_path.write_text('root = "/srv/x"\n[reader]\ndevice = "scribe-colorsoft"\n')
    load_server_config(cfg_path)  # does not raise
