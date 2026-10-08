"""Conservative local allowlist; a model's risk label never grants permission."""
from __future__ import annotations

import re


def read_only(command: str, shell: str) -> bool:
    command = command.strip()
    if shell == "powershell":
        patterns = (
            r"(?:Get-Location|Get-Date|Get-Process|Get-NetConnectionProfile|Get-NetIPAddress|whoami|hostname)",
            r"Get-Command\s+[A-Za-z0-9_.-]+",
            r"Test-Path -LiteralPath '(?:[^'\r\n]|'')+'",
            r"\[bool\]\(Get-Process -Name '[A-Za-z0-9_.-]+' -ErrorAction SilentlyContinue\)",
        )
    elif shell == "cmd":
        patterns = (r"(?:cd|ver|whoami|hostname|ipconfig)", r"where [A-Za-z0-9_.-]+")
    else:
        patterns = (r"(?:pwd|whoami|hostname|date|ls|ls -la|uname -a|ip addr|ip route)",
                    r"pgrep -x -- '[A-Za-z0-9_.-]+'", r"test -e '[^'\r\n]+'")
    return any(re.fullmatch(pattern, command, re.I) for pattern in patterns)
