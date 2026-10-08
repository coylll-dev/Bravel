from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import Settings, config_path, ConsoleError
from .context import context, shell_name
from .planner import Planner


def doctor(settings: Settings, ui, *, api: bool = False) -> int:
    ui.banner()
    shell = shell_name()
    executable = shutil.which("bravel")
    ui.panel("НАСТРОЙКИ", f"Bravel: {__version__}\nКонфиг: {config_path()}\nAPI: {settings.base_url}\n"
             f"Провайдер: {settings.provider}\nМодель: {settings.model}\nКлюч: {'задан' if settings.api_key and settings.api_key != 'your-api-key' else 'не задан'}\n"
             f"Python: {sys.version.split()[0]}\nОболочка: {shell}\nPATH: {executable or 'bravel не найден — используйте install.py'}")
    root = Path(sys.prefix).parent
    if (root / "bravel-install.json").is_file():
        ui.note(f"Управляемая установка: {root}")
        ui.note("Откат доступен." if (root / "rollback/snapshot.json").is_file() else "Снимка отката пока нет; он создаётся при следующем обновлении.")
    else:
        ui.note("Установка pip/pipx: управление обновлениями через этот менеджер.")
    if os.name == "nt":
        for name in ("powershell", "pwsh"):
            if binary := shutil.which(name):
                try:
                    result = subprocess.run([binary, "-NoProfile", "-NonInteractive", "-Command",
                                             "$p=$PROFILE.CurrentUserAllHosts; Write-Output (Get-ExecutionPolicy); Write-Output $p"],
                                            capture_output=True, text=True, timeout=15)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ConsoleError("Не удалось проверить подключение PowerShell; CLI можно запускать отдельно.") from exc
                lines = result.stdout.strip().splitlines()
                if result.returncode == 0 and len(lines) >= 2:
                    profile = Path(lines[-1])
                    raw = profile.read_bytes() if profile.is_file() else b""
                    installed = "# >>> bravel >>>" in raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig", errors="replace")
                    ui.note(f"{name}: политика {lines[0]}, подключение {'записано' if installed else 'не найдено'}. CLI работает независимо от профиля.")
    else:
        profile = Path.home() / ".bashrc"
        installed = profile.is_file() and "# >>> bravel >>>" in profile.read_text(encoding="utf-8", errors="replace")
        ui.note(f"Bash: подключение {'записано' if installed else 'не найдено'} в {profile}")
    settings.validate_key()
    if api:
        ui.note("Проверка API отправляет короткий запрос к выбранной модели; возможен расход токенов.")
        with ui.busy("Проверяю API и модель…"):
            Planner(settings).make_plan("Проверка связи. Ответь кратко, steps оставь пустым.", {"os": context(shell)["os"], "shell": shell})
        ui.note("API ответил в формате Bravel. Команды не выполнялись.")
    else:
        ui.note("Локальные настройки корректны. Проверить API и модель: bravel doctor --api")
    return 0
