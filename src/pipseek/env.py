"""Find the Python environment the user actually works in, and what's installed there.

pipseek is usually installed with pipx or uv, so its own interpreter is an isolated venv.
To show "installed" badges and to install packages, we target the user's environment:
an explicit --python, else the active virtualenv/conda env, else the first python on PATH.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
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
    candidates = (
        base / "bin" / "python",  # venv and conda on Linux/macOS
        base / "Scripts" / "python.exe",  # venv on Windows
        base / "python.exe",  # conda on Windows keeps python.exe at the env root
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _is_store_stub(path: str) -> bool:
    """The Microsoft Store "python" placeholder, which opens the Store instead of running."""
    return "windowsapps" in path.lower()


def find_python(explicit: Optional[str] = None) -> str:
    if explicit:
        return shutil.which(explicit) or explicit
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        root = os.environ.get(var)
        if root:
            found = _venv_python(root)
            if found:
                return found
    windows = sys.platform == "win32"
    for name in ("python", "python3") if windows else ("python3", "python"):
        found = shutil.which(name)
        if found and not (windows and _is_store_stub(found)):
            return found
    return sys.executable


def format_command(cmd: list[str]) -> str:
    """A command line the user can paste into their shell, quoting paths with spaces."""
    if sys.platform == "win32":
        return subprocess.list2cmdline(cmd)
    return shlex.join(cmd)


_MANAGED_SCRIPT = (
    "import os, sys, sysconfig\n"
    "venv = sys.prefix != getattr(sys, 'base_prefix', sys.prefix)\n"
    "marker = os.path.join(sysconfig.get_paths()['stdlib'], 'EXTERNALLY-MANAGED')\n"
    "print(int(not venv and os.path.exists(marker)))\n"
)


class PythonEnv:
    def __init__(self, python: Optional[str] = None) -> None:
        self.python = find_python(python)
        self._installed: Optional[dict[str, str]] = None
        self._managed: Optional[bool] = None
        self._lock = threading.Lock()

    @property
    def externally_managed(self) -> bool:
        """True for an OS-managed Python (PEP 668), where pip shouldn't install packages."""
        if self._managed is None:
            try:
                out = subprocess.run(
                    [self.python, "-c", _MANAGED_SCRIPT],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                ).stdout.strip()
                self._managed = out == "1"
            except (OSError, subprocess.SubprocessError):
                self._managed = False
        return self._managed

    @property
    def installed(self) -> dict[str, str]:
        """Normalized name -> version for every distribution in the target environment.

        Search fetches packages from several threads at once, so the first caller lists the
        environment while the others wait, and the dict is only published once it's complete.
        """
        installed = self._installed
        if installed is not None:
            return installed
        with self._lock:
            if self._installed is None:
                self._installed = self._list_installed()
            return self._installed

    def _list_installed(self) -> dict[str, str]:
        try:
            out = subprocess.run(
                [self.python, "-c", _LIST_SCRIPT],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            ).stdout
            return {normalize(k): v for k, v in json.loads(out).items()}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}

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
        with self._lock:
            self._installed = None
        return result.returncode
