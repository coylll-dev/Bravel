"""Small, read-only discovery of installed Steam games; no broad disk scans."""
from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .planner import Plan, Step


@dataclass(frozen=True)
class SteamGame:
    executable: Path
    library: Path
    app_id: str
    name: str


def steam_roots() -> list[Path]:
    roots = []
    if os.name == "nt":
        import winreg
        for hive, key, value in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key) as handle:
                    path, _ = winreg.QueryValueEx(handle, value)
                    roots.append(Path(path))
            except OSError:
                pass
        for key in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(key):
                roots.append(Path(os.environ[key]) / "Steam")
    else:
        roots.extend([Path.home() / ".steam/steam", Path.home() / ".local/share/Steam", Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"])
    return list(dict.fromkeys(roots))


def find_steam_game(app_id: str, roots: list[Path] | None = None) -> SteamGame | None:
    for root in roots if roots is not None else steam_roots():
        executable = root / "steam.exe" if os.name == "nt" else Path(shutil.which("steam") or str(root / "steam.sh"))
        if not executable.is_file():
            continue
        libraries = [root]
        try:
            vdf = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
            for value in re.findall(r'"path"\s*"([^"\n]+)"', vdf):
                libraries.append(Path(value.replace("\\\\", "\\")))
        except OSError:
            pass
        for library in libraries:
            try:
                manifest = (library / f"steamapps/appmanifest_{app_id}.acf").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r'"name"\s*"([^"\n]+)"', manifest)
            directory = re.search(r'"installdir"\s*"([^"\n]+)"', manifest)
            if not directory or not (library / "steamapps/common" / directory[1]).is_dir():
                continue
            return SteamGame(executable, library, app_id, match[1] if match else f"Steam app {app_id}")
    return None


def game_plan(prompt: str, shell: str) -> Plan | None:
    if not re.search(r"\b(открой|запусти|open|launch|start)\b", prompt, re.I):
        return None
    if not re.search(r"\b(кс|кс2|cs|cs2|counter[ -]?strike)\b", prompt, re.I):
        return None
    game = find_steam_game("730")
    if not game:
        return Plan("Counter-Strike не найден в доступных библиотеках Steam. Проверьте установку Steam и игры; для нестандартного клиента укажите путь в запросе.", ())
    if shell == "powershell":
        path = str(game.executable).replace("'", "''")
        command = f"Start-Process -FilePath '{path}' -ArgumentList '-applaunch', '{game.app_id}'"
    elif shell == "bash":
        command = f"{shlex.quote(str(game.executable))} -applaunch {game.app_id}"
    else:
        # CMD escaping is intentionally not inferred from a user-controlled path.
        return None
    return Plan(f"Найдена игра: {game.name}\nБиблиотека: {game.library}", (Step(command, "Запустить установленную игру через Steam. Steam может обновить игру перед запуском."),))
