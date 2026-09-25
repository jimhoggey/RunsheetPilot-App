"""Shared by the seller tools: where the signing key lives, and the Python they run under.

Kept runnable on macOS's own Python 3.9 (no newer syntax), since that is
what `python3` usually is before use_venv() switches to the repo's .venv.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# In the home folder: outside the repo, so git can never pick it up, and not
# synced to iCloud. Easy to find in Finder (Go → Home).
KEY_DIR = Path.home() / "Runsheet Pilot Licence Key"
PRIVATE_PATH = KEY_DIR / "license_private_key.b64"
PUBLIC_PATH = KEY_DIR / "license_public_key.b64"

_RETRIED = "RP_TOOLS_IN_VENV"


def _base_python() -> str:
    """A Python new enough to build .venv with (the app needs 3.11+)."""
    if sys.version_info >= (3, 11):
        return sys.executable
    for name in ("python3.14", "python3.13", "python3.12", "python3.11"):
        found = shutil.which(name)
        if found:
            return found
    sys.exit("Runsheet Pilot needs Python 3.11 or newer: https://www.python.org/downloads/")


def use_venv() -> None:
    """Re-run the calling script under the repo's .venv, creating it and
    installing requirements.txt when the app won't import there. Uses uv
    when it's installed (a uv-made .venv has no pip)."""
    venv = ROOT / ".venv"
    if os.environ.get(_RETRIED) or Path(sys.prefix).resolve() == venv.resolve():
        return
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    uv = shutil.which("uv")
    if not python.exists():
        print("First run: setting up the project's Python in .venv …")
        make = [uv, "venv", "--python", "3.12", str(venv)] if uv else [_base_python(), "-m", "venv", str(venv)]
        subprocess.run(make, check=True)
    probe = [str(python), "-c", "import propresenterrunsheet.licensing"]
    if subprocess.run(probe, cwd=ROOT, capture_output=True).returncode:
        print("Installing the packages from requirements.txt …")
        install = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install"]
        subprocess.run([*install, "--quiet", "-r", str(ROOT / "requirements.txt")], check=True)
    rerun = subprocess.run([str(python), *sys.argv], env={**os.environ, _RETRIED: "1"})
    sys.exit(rerun.returncode)
