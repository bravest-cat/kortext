"""
Tests for Kortext Guided Setup Wizard, Diagnostics, and Substrate Provisioning.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from kortext.setup import (
    get_kortext_home,
    is_port_open,
    is_unix_socket_open,
    provision_directories,
    run_diagnostics,
    SetupDiagnostics,
    start_substrates,
    stop_substrates,
)
from kortext.cli import main


def test_get_kortext_home():
    home = get_kortext_home()
    assert isinstance(home, Path)
    assert home.name == ".kortext" or "kortext" in str(home).lower()


def test_provision_directories(tmp_path):
    created = provision_directories(tmp_path)
    assert len(created) >= 7
    assert (tmp_path / "run").is_dir()
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "config").is_dir()
    assert (tmp_path / "bin").is_dir()
    assert (tmp_path / "qdrant").is_dir()
    assert (tmp_path / "data" / "falkordb").is_dir()


def test_run_diagnostics():
    diag = run_diagnostics()
    assert isinstance(diag, SetupDiagnostics)
    assert diag.os_name in ("Darwin", "Linux", "Windows")
    assert isinstance(diag.python_compatible, bool)
    assert isinstance(diag.installed_grammars, tuple)
    assert Path(diag.kortext_home).exists()


def test_is_port_open_closed():
    # An improbable local port should return False
    assert is_port_open("127.0.0.1", 59999, timeout=0.1) is False


def test_is_unix_socket_open_nonexistent(tmp_path):
    fake_sock = tmp_path / "nonexistent.sock"
    assert is_unix_socket_open(fake_sock) is False


def test_cli_setup_parser(capsys):
    with patch("sys.argv", ["kortext", "setup", "--yes"]):
        main()
    captured = capsys.readouterr()
    assert "GUIDED SETUP WIZARD" in captured.out
    assert "SETUP COMPLETE" in captured.out


def test_cli_status_parser(capsys):
    with patch("sys.argv", ["kortext", "status"]):
        main()
    captured = capsys.readouterr()
    assert "Kortext Substrate Health" in captured.out
    assert "FalkorDB" in captured.out


def test_provision_configurations(tmp_path):
    from kortext.setup import provision_configurations
    created = provision_configurations(tmp_path)
    assert len(created) >= 2
    falkor_conf = tmp_path / "config" / "falkordb.conf"
    qdrant_yaml = tmp_path / "config" / "qdrant.yaml"
    assert falkor_conf.is_file()
    assert qdrant_yaml.is_file()
    assert str(tmp_path) in falkor_conf.read_text(encoding="utf-8")
    assert str(tmp_path) in qdrant_yaml.read_text(encoding="utf-8")


def test_provision_binaries_mocked(tmp_path):
    from kortext.setup import provision_binaries
    from kortext.binaries import BinaryProvisionResult

    dummy_res = BinaryProvisionResult(
        binary_name="qdrant",
        path=tmp_path / "bin" / "qdrant",
        success=True,
        message="Installed dummy qdrant",
    )
    with patch("kortext.setup.pull_qdrant_binary", return_value=dummy_res), \
         patch("kortext.setup.pull_falkordb_module", return_value=dummy_res):
        results = provision_binaries(tmp_path, force=True)
        assert "qdrant" in results
        assert "falkordb" in results
        assert results["qdrant"].success is True


def test_start_substrates_mocked(tmp_path):
    home = tmp_path / ".kortext"

    with patch("kortext.setup.get_kortext_home", return_value=home), \
         patch("kortext.setup.is_unix_socket_open", return_value=False), \
         patch("kortext.setup.is_port_open", return_value=False), \
         patch("shutil.which", return_value="/usr/local/bin/redis-server"), \
         patch("subprocess.Popen") as mock_popen, \
         patch("subprocess.run") as mock_run:
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_popen.return_value = mock_proc

        # Mock port checks
        calls = {"count": 0}
        def fake_port(host, port, timeout=0.3):
            calls["count"] += 1
            # Return False on first check (service not running yet), True thereafter
            return calls["count"] > 2

        with patch("kortext.setup.is_port_open", side_effect=fake_port):
            success, msg = start_substrates(home)
            assert success is True
            assert "started" in msg.lower() or "running" in msg.lower() or "spawned" in msg.lower()


def test_stop_substrates_mocked(tmp_path):
    home = tmp_path / ".kortext"
    run_dir = home / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "falkordb.pid").write_text("12345")
    (run_dir / "qdrant.pid").write_text("67890")

    with patch("kortext.setup.get_kortext_home", return_value=home), \
         patch("os.kill") as mock_kill:
        success, msg = stop_substrates(home)
        assert success is True
        assert not (run_dir / "falkordb.pid").exists()
        assert not (run_dir / "qdrant.pid").exists()


def test_cli_setup_parser_skip_binaries(capsys):
    with patch("sys.argv", ["kortext", "setup", "--yes", "--skip-binaries"]):
        main()
    captured = capsys.readouterr()
    assert "GUIDED SETUP WIZARD" in captured.out
    assert "binary pull skipped" in captured.out


def test_run_diagnostics_extended_fields(tmp_path):
    from kortext.setup import run_diagnostics
    diag = run_diagnostics(tmp_path)
    assert isinstance(diag.platform_details, str)
    assert isinstance(diag.qdrant_binary_present, bool)
    assert isinstance(diag.falkordb_binary_present, bool)
    assert isinstance(diag.redis_server_present, bool)
    assert isinstance(diag.missing_dependencies, tuple)


