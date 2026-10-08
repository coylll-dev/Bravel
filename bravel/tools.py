"""Typed agent tools: bounded reads and explicitly approved file writes."""
from __future__ import annotations

import fnmatch
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import ConsoleError
from .privacy import redact


SCHEMAS = {
    "games": {"description": "Installed Steam games; does not launch anything", "arguments": {}},
    "apps": {"description": "Launchable apps discovered in PATH and standard locations, not all OS packages", "arguments": {}},
    "system_info": {"description": "OS, CPU count, disk space for current directory", "arguments": {}},
    "processes": {"description": "Current process names/PIDs, without command-line arguments", "arguments": {}},
    "network_info": {"description": "Local IP addresses and interfaces; not public Internet IP", "arguments": {}},
    "list_directory": {"description": "Directory entries, types and sizes; does not read contents", "arguments": {"path": "optional directory path"}},
    "find_files": {"description": "Bounded filename search (depth 5, 3 seconds), no contents", "arguments": {"path": "optional root", "pattern": "filename glob, e.g. *.txt"}},
    "read_text": {"description": "Read UTF-8 text (up to 32 KB); requires user approval, rejects credential files", "arguments": {"path": "file path"}},
    "write_text": {"description": "Create/replace UTF-8 file with full preview; requires RUN; old file copied to a new backup", "arguments": {"path": "file path", "content": "complete text, up to 8000 characters"}},
}


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


def validate(call: ToolCall) -> None:
    if call.name not in SCHEMAS or not isinstance(call.arguments, dict):
        raise ConsoleError("Неизвестный инструмент агента")
    allowed = set(SCHEMAS[call.name]["arguments"])
    if not set(call.arguments) <= allowed or any(not isinstance(value, str) for value in call.arguments.values()):
        raise ConsoleError("Неверные параметры инструмента")
    for key in ("path", "pattern"):
        if key in call.arguments and (not call.arguments[key] or len(call.arguments[key]) > 2000 or any(c in call.arguments[key] for c in "\0\r\n")):
            raise ConsoleError("Неверный путь или шаблон инструмента")
    if call.name in {"read_text", "write_text"} and not call.arguments.get("path"):
        raise ConsoleError("Инструменту нужен путь файла")
    if call.name == "write_text" and ("content" not in call.arguments or len(call.arguments["content"]) > 8000):
        raise ConsoleError("Текст файла должен быть не длиннее 8000 символов")
    if call.name == "write_text" and redact(call.arguments["content"]) != call.arguments["content"]:
        raise ConsoleError("Текст содержит распознанные учётные данные; инструмент не записывает секреты")
    if call.name == "find_files" and (not call.arguments.get("pattern") or any(c in call.arguments["pattern"] for c in "/\\")):
        raise ConsoleError("Нужен шаблон имени файла без пути")


def resolve_path(cwd: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def text_path(cwd: Path, value: str) -> Path:
    path = resolve_path(cwd, value)
    name = path.name.casefold()
    if name.startswith(".env") or name in {"id_rsa", "id_ed25519", "credentials", "credentials.json", "secrets.json"} or path.suffix.casefold() in {".pem", ".key", ".pfx"}:
        raise ConsoleError("Инструменты текста не читают и не изменяют файлы учётных данных")
    return path


def fixed_command(shell: str, command: str) -> str:
    from .executor import shell_argv
    try:
        result = subprocess.run(shell_argv(shell, command), stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=15,
                                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        if result.returncode:
            raise ConsoleError("Не удалось прочитать системные сведения")
        return result.stdout[:32000]
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConsoleError("Не удалось прочитать системные сведения") from exc


def run(call: ToolCall, cwd: Path) -> dict:
    validate(call)
    args = call.arguments
    if call.name == "games":
        from .apps import installed_steam_games
        return {"scope": "Steam libraries only", "limit": 200, "games": [{"name": game.name, "app_id": game.app_id,
                 "steam_executable": str(game.executable)} for game in installed_steam_games()[:200]]}
    if call.name == "apps":
        from .programs import installed_applications
        return {"scope": "launchable apps in standard locations", "limit": 200, "apps": [{"name": app.name, "executable": str(app.executable)} for app in installed_applications()[:200]]}
    if call.name == "system_info":
        disk = shutil.disk_usage(cwd)
        return {"os": platform.system(), "release": platform.release(), "architecture": platform.machine(),
                "cpu_count": os.cpu_count(), "disk_total_bytes": disk.total, "disk_free_bytes": disk.free}
    if call.name == "processes":
        if os.name == "nt":
            return {"processes": json.loads(fixed_command("powershell", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Process | Select-Object -First 200 Id,ProcessName | ConvertTo-Json -Compress"))}
        entries = []
        for path in list(Path("/proc").glob("[0-9]*"))[:200]:
            try:
                entries.append({"pid": int(path.name), "name": (path / "comm").read_text().strip()})
            except OSError:
                pass
        return {"processes": entries, "limit": 200}
    if call.name == "network_info":
        command = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-NetIPAddress | Select-Object -First 100 InterfaceAlias,IPAddress,AddressFamily | ConvertTo-Json -Compress" if os.name == "nt" else "ip -brief address"
        return {"local_interfaces": fixed_command("powershell" if os.name == "nt" else "bash", command)}
    path = resolve_path(cwd, args.get("path", "."))
    if call.name == "list_directory":
        entries = []
        for entry in sorted(path.iterdir(), key=lambda entry: entry.name.casefold())[:200]:
            try:
                entries.append({"name": entry.name, "directory": entry.is_dir(), "bytes": entry.stat().st_size})
            except OSError:
                pass
        return {"path": str(path), "entries": entries, "limit": 200}
    if call.name == "find_files":
        matches, visited = [], 0
        deadline = time.monotonic() + 3
        if not path.is_dir():
            raise ConsoleError("Папка поиска не найдена")
        limited = False
        for directory, dirs, files in os.walk(path, followlinks=False):
            depth = len(Path(directory).relative_to(path).parts)
            dirs[:] = [name for name in dirs if name not in {".git", ".venv", "node_modules"} and not (Path(directory) / name).is_symlink()] if depth < 5 else []
            visited += len(files)
            matches += [str(Path(directory) / name) for name in files if fnmatch.fnmatch(name, args["pattern"])]
            if len(matches) >= 200 or visited > 5000 or time.monotonic() > deadline:
                limited = True
                break
        return {"matches": matches[:200], "limited": limited, "max_depth": 5, "skipped_directories": [".git", ".venv", "node_modules"]}
    path = text_path(cwd, args["path"])
    if call.name == "read_text":
        import codecs
        with path.open("rb") as file:
            data = file.read(32001)
        decoder = codecs.getincrementaldecoder("utf-8-sig")()
        return {"path": str(path), "text": decoder.decode(data[:32000], final=len(data) <= 32000), "truncated": len(data) > 32000}
    if call.name == "write_text":
        raw = Path(args["path"]).expanduser()
        raw = raw if raw.is_absolute() else cwd / raw
        if raw.is_symlink() or "[СКРЫТО]" in args["content"]:
            raise ConsoleError("Запись ссылки или замаскированного текста не поддерживается")
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if path.exists():
            with tempfile.NamedTemporaryFile(prefix=path.name + ".bravel-backup-", dir=path.parent, delete=False) as file:
                backup = Path(file.name)
            shutil.copy2(path, backup)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as file:
            temporary = Path(file.name)
            file.write(args["content"])
        try:
            if path.exists():
                shutil.copymode(path, temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(path), "written": True, "bytes": path.stat().st_size,
                "backup": str(backup) if backup else None, "verified": path.read_text(encoding="utf-8") == args["content"]}
    raise ConsoleError("Инструмент не найден")
