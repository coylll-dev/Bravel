"""Bounded application discovery in PATH, App Paths and standard install folders."""
from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .planner import Plan, Step


@dataclass(frozen=True)
class Application:
    name: str
    executable: Path
    aliases: tuple[str, ...] = ()


KNOWN = (
    ("Visual Studio Code", ("vscode", "vs code", "vs-code", "code", "вскод"), ("code",), "Microsoft VS Code/Code.exe"),
    ("Google Chrome", ("chrome", "хром", "гугл хром"), ("google-chrome", "chrome", "chromium"), "Google/Chrome/Application/chrome.exe"),
    ("Mozilla Firefox", ("firefox", "фаерфокс", "файрфокс"), ("firefox",), "Mozilla Firefox/firefox.exe"),
    ("Notepad", ("notepad", "блокнот"), ("notepad",), ""),
    ("VLC", ("vlc",), ("vlc",), "VideoLAN/VLC/vlc.exe"),
)


def app_paths() -> list[Path]:
    if os.name != "nt":
        return []
    import winreg
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths", 0, winreg.KEY_READ | view) as parent:
                    for index in range(min(winreg.QueryInfoKey(parent)[0], 1000)):
                        try:
                            name = winreg.EnumKey(parent, index)
                            with winreg.OpenKey(parent, name) as key:
                                path, _ = winreg.QueryValueEx(key, "")
                                found.append(Path(os.path.expandvars(path.strip('"'))))
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def installed_applications() -> list[Application]:
    result = {}
    roots = [Path(os.environ[name]) for name in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(name)]
    if os.environ.get("LOCALAPPDATA"):
        roots += [Path(os.environ["LOCALAPPDATA"]), Path(os.environ["LOCALAPPDATA"]) / "Programs"]
    registered = app_paths()
    for title, aliases, commands, relative in KNOWN:
        candidates = [Path(value) for command in commands if (value := shutil.which(command))]
        if title == "Visual Studio Code" and os.name == "nt":
            candidates = [path.parent.parent / "Code.exe" for path in candidates] + candidates
        if relative and os.name == "nt":
            # Prefer the actual executable to a .cmd wrapper such as code.cmd.
            candidates = [root / relative for root in roots] + candidates
        candidates = [path for path in candidates if path.is_file()]
        if candidates:
            path = candidates[0].resolve()
            result[title.casefold()] = Application(title, path, aliases)
    for path in registered:
        if path.is_file() and path.suffix.casefold() == ".exe" and all(c not in str(path) for c in "\r\n"):
            title = path.stem
            if not any(app.executable == path.resolve() for app in result.values()):
                result[title.casefold()] = Application(title, path.resolve(), (title.casefold(),))
    if os.name != "nt":
        # Read desktop metadata, never execute its Exec field or shell substitutions.
        directories = [Path.home() / ".local/share/applications", Path("/usr/share/applications")]
        for directory in directories:
            for desktop in list(directory.glob("*.desktop"))[:1000]:
                try:
                    section = ""
                    values = {}
                    for line in desktop.read_text(encoding="utf-8", errors="replace").splitlines():
                        if line.startswith("["):
                            section = line
                        if section == "[Desktop Entry]" and "=" in line:
                            key, value = line.split("=", 1)
                            values[key] = value
                    if values.get("Type") != "Application" or values.get("Hidden") == "true" or values.get("Terminal") == "true":
                        continue
                    args = shlex.split(values.get("Exec", ""))
                    if not args or any(arg not in {"%u", "%U", "%f", "%F"} for arg in args[1:]):
                        continue  # Only direct executables; do not silently discard needed args.
                    executable = shutil.which(args[0])
                    if executable and (title := values.get("Name", "")) and all(c not in title for c in "\r\n"):
                        result.setdefault(title.casefold(), Application(title, Path(executable).resolve(), (desktop.stem,)))
                except (OSError, ValueError):
                    continue
    return sorted(result.values(), key=lambda app: app.name.casefold())


def launch_plan(prompt: str, shell: str) -> Plan | None:
    if not re.search(r"\b(открой|запусти|open|launch|start)\b", prompt, re.I):
        return None
    matches = []
    for app in installed_applications():
        if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", prompt, re.I) for name in (app.name, *app.aliases)):
            matches.append(app)
    if not matches:
        return None
    if len(matches) > 1:
        return Plan("Найдено несколько приложений: " + ", ".join(app.name for app in matches) + ". Уточните название.", ())
    app = matches[0]
    path = str(app.executable)
    if any(c in path for c in "\r\n"):
        return None
    process = app.executable.stem
    check = ""
    if shell == "powershell":
        command = "Start-Process -FilePath '" + path.replace("'", "''") + "'"
        if re.fullmatch(r"[A-Za-z0-9_.-]+", process):
            check = f"[bool](Get-Process -Name '{process}' -ErrorAction SilentlyContinue)"
    elif shell == "cmd":
        # CMD expands %variables% even inside quotes; reject such paths.
        if any(c in path for c in '%"!'):
            return None
        command = f'start "" "{path}"'
    else:
        command = shlex.quote(path) + " &"
        if shutil.which("pgrep") and re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", app.executable.name):
            check = f"pgrep -x -- '{app.executable.name}'"
    return Plan(f"Найдено приложение: {app.name}\nПуть: {path}",
                (Step(command, "Отправить запрос на запуск приложения. Наличие процесса проверяется отдельно, если доступно.", "low", check),))
