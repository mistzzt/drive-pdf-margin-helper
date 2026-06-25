import pytest

from scribe_crop import cli


@pytest.fixture
def server_config_file(tmp_path):
    root = tmp_path / "ScribeCrop"
    for sub in ("upload", "processed", "failed"):
        (root / sub).mkdir(parents=True)
    (root / "upload" / "a.pdf").write_bytes(b"%PDF")
    cfg = tmp_path / "server.toml"
    cfg.write_text(f'root = "{root}"\nstate_path = "{root / "state.db"}"\n')
    return cfg, root


def test_resolve_binary_found():
    assert cli.resolve_binary("anything", which=lambda _: "/usr/bin/pdfcropmargins") == "/usr/bin/pdfcropmargins"


def test_resolve_binary_missing_raises():
    with pytest.raises(cli.BinaryMissing):
        cli.resolve_binary("pdfcropmargins", which=lambda _: None)


def test_main_fails_clearly_when_binary_absent(server_config_file):
    cfg, _ = server_config_file
    rc = cli.main(["-c", str(cfg), "reconcile"], which=lambda _: None)
    assert rc == 2


def test_reconcile_command_dispatches_and_processes(server_config_file, monkeypatch):
    cfg, root = server_config_file
    calls = []
    from scribe_crop import service as service_mod

    real_reconcile = service_mod.reconcile

    def spy(config, *, store, process, mirror_current):
        calls.append(mirror_current)
        return real_reconcile(config, store=store, process=process, mirror_current=mirror_current)

    monkeypatch.setattr(service_mod, "reconcile", spy)
    # Avoid probing real tools.
    from scribe_crop.fingerprint import ToolVersion

    monkeypatch.setattr(
        "scribe_crop.service.probe_tool_version",
        lambda: ToolVersion("x", "y"),
    )
    rc = cli.main(
        ["-c", str(cfg), "reconcile"],
        which=lambda _: "/bin/pdfcropmargins",
    )
    assert rc == 0
    assert calls == [False]


def test_run_command_invokes_service_run(server_config_file, monkeypatch):
    cfg, _ = server_config_file
    from scribe_crop.fingerprint import ToolVersion

    monkeypatch.setattr(
        "scribe_crop.service.probe_tool_version",
        lambda: ToolVersion("x", "y"),
    )
    ran = {"called": False}
    monkeypatch.setattr(cli.Service, "run", lambda self, **kw: ran.update(called=True))
    rc = cli.main(["-c", str(cfg), "run"], which=lambda _: "/bin/pdfcropmargins")
    assert rc == 0
    assert ran["called"]


def test_run_command_mirror_current_flag(server_config_file, monkeypatch):
    cfg, _ = server_config_file
    from scribe_crop.fingerprint import ToolVersion

    monkeypatch.setattr(
        "scribe_crop.service.probe_tool_version",
        lambda: ToolVersion("x", "y"),
    )
    captured = {}

    def fake_run(self, **kw):
        captured["mirror_current"] = self._readiness.is_current()

    monkeypatch.setattr(cli.Service, "run", fake_run)
    rc = cli.main(
        ["-c", str(cfg), "run", "--mirror-current"],
        which=lambda _: "/bin/pdfcropmargins",
    )
    assert rc == 0
    assert captured["mirror_current"] is True
