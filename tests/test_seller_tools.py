"""The seller tools re-run themselves under the repo's .venv (tools/_shared.py)."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import _shared  # noqa: E402


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """No cryptography, no .venv yet; subprocess.run recorded (the re-run exits 7)."""
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setattr(_shared, "ROOT", tmp_path)
    monkeypatch.delenv(_shared._RETRIED, raising=False)
    monkeypatch.setattr(sys, "argv", ["tools/issue_license.py", "--name", "C3"])
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw))
        return SimpleNamespace(returncode={"-c": 1, "tools/issue_license.py": 7}.get(cmd[1], 0))

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def test_fresh_machine_builds_venv_installs_and_reruns(calls, tmp_path):
    venv_py = str(tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    with pytest.raises(SystemExit) as exit_:
        _shared.use_venv()
    assert exit_.value.code == 7  # the re-run's own exit code
    cmds = [cmd for cmd, _ in calls]
    assert cmds[0][1:] == ["-m", "venv", str(tmp_path / ".venv")]
    assert cmds[2][:4] == [venv_py, "-m", "pip", "install"]
    assert cmds[3] == [venv_py, "tools/issue_license.py", "--name", "C3"]
    assert calls[3][1]["env"][_shared._RETRIED] == "1"


def test_still_missing_inside_venv_stops_instead_of_looping(calls, monkeypatch):
    monkeypatch.setenv(_shared._RETRIED, "1")
    with pytest.raises(ImportError):
        _shared.use_venv()
    assert calls == []


def test_packages_present_is_a_no_op(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("should not run"))
    _shared.use_venv()
