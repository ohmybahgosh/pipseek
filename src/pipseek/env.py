"""Find the Python environment the user actually works in, and what's installed there.

pipseek is usually installed with pipx or uv, so its own interpreter is an isolated venv.
To show "installed" badges and to install packages, we target the user's environment:
an explicit --python, else the active virtualenv/conda env, else the first python on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from pipseek.index import normalize

_LIST_SCRIPT = (
    "import json, importlib.metadata as m\n"
    "out = {}\n"
    "for d in m.distributions():\n"
    "    n = d.metadata.get('Name')\n"
    "    if n: out[n] = d.version\n"
    "print(json.dumps(out))\n"
)


def _venv_python(root: str) -> Optional[str]:
    base = Path(root)
    for candidate in (base / "bin" / "python", base / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    return None


def find_python(explicit: Optional[str] = None) -> str:
    if explicit:
        return shutil.which(explicit) or explicit
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = os.environ.get(var)
        if root:
            found = _venv_python(root)
            if found:
                return found
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


class PythonEnv:
    def __init__(self, python: Optional[str] = None) -> None:
        self.python = find_python(python)
        self._installed: Optional[dict[str, str]] = None

    @property
    def installed(self) -> dict[str, str]:
        """Normalized name -> version for every distribution in the target environment."""
        if self._installed is None:
            self._installed = {}
            try:
                out = subprocess.run(
                    [self.python, "-c", _LIST_SCRIPT],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True,
                ).stdout
                self._installed = {normalize(k): v for k, v in json.loads(out).items()}
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        return self._installed

    def version_of(self, name: str) -> Optional[str]:
        return self.installed.get(normalize(name))

    def install_command(self, name: str, upgrade: bool = False) -> list[str]:
        cmd = [self.python, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.append(name)
        return cmd

    def install(self, name: str, upgrade: bool = False) -> int:
        result = subprocess.run(self.install_command(name, upgrade))
        self._installed = None
        return result.returncode
