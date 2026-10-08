from __future__ import annotations

import os
import platform
import shutil


def shell_name(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    return "powershell" if os.name == "nt" else "bash"


def context(shell: str) -> dict:
    known = ("cmd", "pwsh", "powershell", "bash", "git", "python", "python3", "steam", "nmcli", "ip", "netsh", "winget", "xdg-open")
    return {
        "os": platform.system(),
        "shell": shell,
        "cwd": os.getcwd(),
        "available_commands": [name for name in known if shutil.which(name)],
        "command_stdin_interactive": False,
        # No environment, file contents, shell history, or home-directory inventory.
    }
