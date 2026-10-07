from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import ConsoleError


def launch() -> int:
    filename = "Bravel.Desktop.exe" if os.name == "nt" else "Bravel.Desktop"
    candidates = [Path(sys.prefix).parent / "desktop" / filename]
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").exists():
            candidates.append(parent / "dist/desktop" / filename)
    for candidate in candidates:
        if candidate.is_file():
            environment = os.environ.copy()
            environment["BRAVEL_PYTHON"] = sys.executable
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            subprocess.Popen([str(candidate)], env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
            return 0
    raise ConsoleError("Интерфейс ещё не установлен. Выполните python install.py update --desktop (на Linux: python3).")
