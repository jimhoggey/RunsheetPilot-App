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
    """No .venv yet and the app won't import; subprocess.run recorded (the re-run exits 7)."""
    monkeypatch.setattr(_shared, "ROOT", tmp_path)
    monkeypatch.delenv(_shared._RETRIED, raising=False)
    monkeypatch.setattr(sys, "argv", ["tools/issue_license.py", "--name", "C3"])
    monkeypatch.setattr(_shared.shutil, "which", lambda name: None)
    seen = []

    def run(cmd, **kw):
        seen.append((cmd, kw))
        return SimpleNamespace(returncode={"-c": 1, "tools/issue_license.py": 7}.get(cmd[1], 0))

    monkeypatch.setattr(subprocess, "run", run)
    return seen


def _venv_py(root):
    return str(root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def test_fresh_machine_builds_venv_installs_and_reruns(calls, tmp_path):
    venv_py = _venv_py(tmp_path)
    with pytest.raises(SystemExit) as exit_:
        _shared.use_venv()
    assert exit_.value.code == 7  # the re-run's own exit code
    cmds = [cmd for cmd, _ in calls]
    assert cmds[0][1:] == ["-m", "venv", str(tmp_path / ".venv")]
    assert cmds[1] == [venv_py, "-c", "import propresenterrunsheet.licensing"]  # the whole app, not one package
    assert cmds[2][:4] == [venv_py, "-m", "pip", "install"]
    assert cmds[3] == [venv_py, "tools/issue_license.py", "--name", "C3"]
    assert calls[3][1]["env"][_shared._RETRIED] == "1"


def test_uses_uv_when_installed(calls, monkeypatch, tmp_path):
    monkeypatch.setattr(_shared.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    with pytest.raises(SystemExit):
        _shared.use_venv()
    cmds = [cmd for cmd, _ in calls]
    assert cmds[0][:2] == ["/bin/uv", "venv"]
    assert cmds[2][:5] == ["/bin/uv", "pip", "install", "--python", _venv_py(tmp_path)]


def test_no_new_enough_python_says_so(monkeypatch):
    monkeypatch.setattr(sys, "version_info", (3, 9, 6))
    monkeypatch.setattr(_shared.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit, match="3.11 or newer"):
        _shared._base_python()


@pytest.mark.parametrize("inside", ["rerun", "venv"])
def test_no_rerun_once_inside_venv(calls, monkeypatch, tmp_path, inside):
    if inside == "rerun":
        monkeypatch.setenv(_shared._RETRIED, "1")
    else:
        monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    _shared.use_venv()
    assert calls == []
