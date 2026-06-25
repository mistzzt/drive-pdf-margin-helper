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
    p.write_text("[crop]\npercent_retain = 8\nuniform = true\npre_crop = 5\n")
    result = load_drive_config(p)
    assert result.ok
    assert result.error is None
    assert result.crop == {"percent_retain": 8, "uniform": True, "pre_crop": 5}
    assert result.raw_bytes == p.read_bytes()


def test_drive_config_absent_is_empty_not_error(tmp_path):
    result = load_drive_config(tmp_path / "missing.toml")
    assert result.ok
    assert result.crop == {}
    assert result.raw_bytes == b""


def test_drive_config_parse_error_falls_back(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[crop\npercent_retain = ")
    result = load_drive_config(p)
    assert not result.ok
    assert result.crop is None
    assert "parse error" in result.error
    assert result.raw_bytes == p.read_bytes()


def test_drive_config_unknown_key_falls_back(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[crop]\nbogus = 1\n")
    result = load_drive_config(p)
    assert not result.ok
    assert result.crop is None
    assert "bogus" in result.error


def test_drive_config_bad_type_falls_back(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[crop]\nuniform = "yes"\n')
    result = load_drive_config(p)
    assert not result.ok
    assert result.crop is None


def test_drive_config_crop_not_table(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("crop = 5\n")
    result = load_drive_config(p)
    assert not result.ok
    assert "table" in result.error
