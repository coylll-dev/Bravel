#!/usr/bin/env python3
"""Bravel installer: isolated, per-user, Windows and Linux, no Git required."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import venv
import tempfile
import platform
import zipfile
import re
from urllib.request import Request, urlopen
from pathlib import Path

SOURCE_URL = "https://github.com/coylll-dev/Bravel/archive/refs/heads/main.zip"
BEGIN, END = "# >>> bravel >>>", "# <<< bravel <<<"


def default_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Bravel"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "bravel"


def profile_text(path: Path) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes()
    return raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig").replace("\r\n", "\n")


def remove_block(text: str) -> str:
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise RuntimeError("Повреждены маркеры Bravel в профиле; файл не изменён")
    if BEGIN not in text:
        return text
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END)
    if end < len(text) and text[end] == "\n":
        end += 1
    return text[:start] + text[end:]


def write_profile(path: Path, block: str | None) -> None:
    original = profile_text(path)
    updated = remove_block(original)
    if block is not None:
        updated += ("" if not updated or updated.endswith("\n") else "\n") + block + "\n"
    if original == updated:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bravel.bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    was_utf16 = path.exists() and path.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff"))
    path.write_text(updated, encoding="utf-16" if was_utf16 else ("utf-8-sig" if path.suffix == ".ps1" else "utf-8"))


def shell_block(root: Path, shell: str) -> str:
    scripts = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
    hook = root / "hooks" / ("bravel.ps1" if shell == "powershell" else "bravel.bash")
    if shell == "powershell":
        escape = lambda value: str(value).replace("'", "''")
        exe = scripts / ("bravel.exe" if os.name == "nt" else "bravel")
        body = f"if (Test-Path -LiteralPath '{escape(exe)}') {{\n    $env:PATH = '{escape(scripts)}' + [IO.Path]::PathSeparator + $env:PATH\n    . '{escape(hook)}'\n}}"
    else:
        body = f"if [[ -x {shlex.quote(str(scripts / 'bravel'))} ]]; then\n    export PATH={shlex.quote(str(scripts))}:\"$PATH\"\n    source {shlex.quote(str(hook))}\nfi"
    return BEGIN + "\n" + body + "\n" + END


def detect_profiles(shell: str) -> list[tuple[str, Path]]:
    shell = ("powershell" if os.name == "nt" else "bash") if shell == "auto" else shell
    if shell == "bash":
        return [(shell, Path.home() / ".bashrc")]
    profiles = []
    for name in ("pwsh", "powershell"):
        executable = shutil.which(name)
        if not executable:
            continue
        result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $PROFILE.CurrentUserAllHosts"], capture_output=True, encoding="utf-8", check=True)
        path = Path(result.stdout.strip())
        if not path.is_absolute():
            raise RuntimeError("PowerShell вернул некорректный путь профиля")
        if path not in [item[1] for item in profiles]:
            profiles.append((shell, path))
    if not profiles:
        raise RuntimeError("PowerShell не найден; выберите --shell bash")
    return profiles


def load_manifest(root: Path) -> dict:
    path = root / "bravel-install.json"
    if not path.is_file() or root.is_symlink():
        raise RuntimeError(f"Не управляемая установка Bravel: {root}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("app") != "bravel" or Path(data.get("root", "")).resolve() != root.resolve():
        raise RuntimeError("Некорректный манифест Bravel")
    if root.resolve() in {Path.home().resolve(), Path.cwd().resolve(), Path(root.anchor)}:
        raise RuntimeError("Небезопасная папка установки")
    return data


def configure_command(python: Path, provider: str | None) -> list[str]:
    command = [str(python), "-m", "bravel", "configure"]
    if provider is not None:
        command.extend(["--provider", provider])
    return command


def usage_instructions(root: Path, profiles: list[tuple[str, Path]]) -> None:
    shells = {shell for shell, _ in profiles}
    if "powershell" in shells:
        print("  Откройте PowerShell. Из CMD: powershell (или pwsh для PowerShell 7).")
        print("  Автоматические # запросы и исправления работают в PowerShell; CMD поддерживает только прямой CLI.")
        if os.name == "nt":
            for name in ("powershell", "pwsh"):
                executable = shutil.which(name)
                if executable:
                    result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"], capture_output=True, text=True)
                    if result.returncode == 0 and result.stdout.strip() in {"Restricted", "AllSigned"}:
                        print(f"  ! {name}: политика {result.stdout.strip()} может блокировать профиль Bravel.")
                        print("  Если вы разрешаете локальные скрипты, выполните в этой оболочке: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned")
                        print("  Затем откройте новую сессию PowerShell. Политика не изменялась установщиком.")
    if "bash" in shells:
        print("  Откройте новую интерактивную сессию Bash.")
    if not shells:
        print("  Подключение оболочки отключено; запускайте CLI из папки venv/Scripts или venv/bin установки.")
    print('  Проверка: bravel doctor. Запрос: ai "покажи сетевые подключения" или # помоги')
    print("  Управление: bravel configure, bravel update, bravel uninstall")
    if os.name == "nt":
        executable = root / "venv/Scripts/bravel.exe"
        print(f'  CLI из CMD: "{executable}" ask --shell cmd "помоги"')
        print(f'  Короткая команда на текущую сессию CMD: doskey bravel="{executable}" $*')


def desktop_shortcut(root: Path) -> list[dict]:
    if os.name == "nt":
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            return []
        # Command strings work without profiles or changes to execution policy.
        quote = lambda value: str(value).replace("'", "''")
        command = ("$folder=[Environment]::GetFolderPath('Programs'); "
                   "$path=Join-Path $folder 'Bravel.lnk'; "
                   "$link=(New-Object -ComObject WScript.Shell).CreateShortcut($path); "
                   f"$target='{quote(root / 'desktop/Bravel.Desktop.exe')}'; "
                   "if ((Test-Path -LiteralPath $path) -and $link.TargetPath -ne $target) { exit 0 }; "
                   "$link.TargetPath=$target; "
                   f"$link.WorkingDirectory='{quote(Path.home())}'; "
                   "$link.Description='Bravel · агент на компьютере'; $link.Save(); "
                   "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Output $path")
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, encoding="utf-8")
        if result.returncode == 0 and result.stdout.strip():
            return [{"path": result.stdout.strip(), "kind": "lnk"}]
        return []
    path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "applications/bravel.desktop"
    signature = f"# Bravel-managed: {root.resolve()}\n"
    if path.exists() and not path.read_text(encoding="utf-8").startswith(signature):
        return []
    executable = str(root / "desktop/Bravel.Desktop").replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(signature + f'[Desktop Entry]\nType=Application\nName=Bravel\nComment=AI desktop agent\nExec="{executable}"\nTerminal=false\nCategories=Utility;\n', encoding="utf-8")
    return [{"path": str(path), "kind": "desktop"}]


def remove_desktop_shortcuts(root: Path, manifest: dict) -> None:
    for item in manifest.get("shortcuts", []):
        path = Path(item["path"])
        if not path.is_file():
            continue
        if item["kind"] == "desktop":
            if path.read_text(encoding="utf-8").startswith(f"# Bravel-managed: {root.resolve()}\n"):
                path.unlink()
        elif item["kind"] == "lnk" and os.name == "nt":
            shell = shutil.which("powershell") or shutil.which("pwsh")
            if shell:
                quote = lambda value: str(value).replace("'", "''")
                command = (f"$path='{quote(path)}'; $link=(New-Object -ComObject WScript.Shell).CreateShortcut($path); "
                           f"if ($link.TargetPath -eq '{quote(root / 'desktop/Bravel.Desktop.exe')}') {{ Remove-Item -LiteralPath $path }}")
                subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], check=True)


def install_desktop(root: Path, python: Path, source: Path | None = None) -> None:
    filename = "Bravel.Desktop.exe" if os.name == "nt" else "Bravel.Desktop"
    destination = root / "desktop"
    if source is not None:
        source = source.resolve()
        if not (source / filename).is_file():
            raise RuntimeError(f"Сборка интерфейса не найдена: {source}")
        if source != destination.resolve():
            shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        if platform.machine().lower() not in {"amd64", "x86_64"}:
            raise RuntimeError("Готовый интерфейс выпускается для x64; для другой архитектуры соберите C# проект")
        version = subprocess.run([str(python), "-m", "bravel", "--version"], capture_output=True, text=True, check=True).stdout.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise RuntimeError("Не удалось определить версию Bravel")
        target = "win-x64" if os.name == "nt" else "linux-x64"
        url = f"https://github.com/coylll-dev/Bravel/releases/download/v{version}/bravel-desktop-{target}.zip"
        with tempfile.TemporaryDirectory(prefix="bravel-desktop-", dir=root) as directory:
            archive_path = Path(directory) / "desktop.zip"
            request = Request(url, headers={"User-Agent": "Bravel-installer"})
            with urlopen(request, timeout=60) as response, archive_path.open("wb") as file:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > 300_000_000:
                        raise RuntimeError("Архив интерфейса слишком большой")
                    file.write(chunk)
            staging = Path(directory) / "files"
            staging.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                if sum(item.file_size for item in archive.infolist()) > 600_000_000:
                    raise RuntimeError("Распакованный интерфейс слишком большой")
                for item in archive.infolist():
                    candidate = (staging / item.filename).resolve()
                    if staging.resolve() not in candidate.parents or (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise RuntimeError("Небезопасный путь в архиве интерфейса")
                archive.extractall(staging)
            if not (staging / filename).is_file():
                raise RuntimeError("Неверный архив интерфейса")
            shutil.copytree(staging, destination, dirs_exist_ok=True)
    if os.name != "nt":
        (destination / filename).chmod(0o755)
    print("  ✓ Интерфейс установлен. Запуск: bravel desktop")


def install(root: Path, *, source: str | None = None, profiles: list[tuple[str, Path]] | None = None, provider: str | None = None, configure: bool = True, desktop: bool = False, desktop_source: Path | None = None) -> None:
    root = root.expanduser().absolute()
    marker = root / "bravel-install.json"
    previous = load_manifest(root) if marker.exists() else None
    if root.is_symlink() or (root.exists() and any(root.iterdir()) and previous is None):
        raise RuntimeError(f"Установка в чужую папку или ссылку запрещена: {root}")
    selected = profiles if profiles is not None else detect_profiles("auto")
    if previous:
        for item in previous["profiles"]:
            if Path(item["path"]) not in [path.absolute() for _, path in selected]:
                selected.append((item["shell"], Path(item["path"])))
    for _, path in selected:
        remove_block(profile_text(path))
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"app": "bravel", "root": str(root.resolve()), "profiles": [{"shell": shell, "path": str(path.absolute())} for shell, path in selected], "source": source or SOURCE_URL, "shortcuts": previous.get("shortcuts", []) if previous else []}, indent=2), encoding="utf-8")
    environment = root / "venv"
    if not environment.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    local = Path(__file__).resolve().parent
    package_source = source or (str(local) if (local / "pyproject.toml").exists() else SOURCE_URL)
    pip_environment = os.environ.copy()
    pip_environment["PYTHONUTF8"] = "1"
    subprocess.run([str(python), "-m", "pip", "install", "--upgrade", package_source], check=True, env=pip_environment)
    hooks = root / "hooks"
    hooks.mkdir(exist_ok=True)
    for suffix, shell in (("ps1", "powershell"), ("bash", "bash")):
        result = subprocess.run([str(python), "-m", "bravel", "integration", shell], check=True, capture_output=True, encoding="utf-8")
        source_hook = Path(result.stdout.strip())
        (hooks / f"bravel.{suffix}").write_text(source_hook.read_text(encoding="utf-8-sig"), encoding="utf-8-sig" if suffix == "ps1" else "utf-8")
    cached = root / "installer.py"
    bundled_installer = environment / "share/bravel/install.py"
    installer_source = bundled_installer if bundled_installer.exists() else Path(__file__).resolve()
    if installer_source.resolve() != cached.resolve():
        shutil.copy2(installer_source, cached)
    for shell, path in selected:
        write_profile(path, shell_block(root, shell))
        print(f"  ✓ Подключение: {path}")
    print(f"  ◆ Bravel установлен: {root}")
    if configure:
        subprocess.run(configure_command(python, provider), check=True)
    if desktop or desktop_source is not None or (previous and previous.get("desktop")):
        install_desktop(root, python, desktop_source)
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["desktop"] = True
        data["shortcuts"] = desktop_shortcut(root)
        marker.write_text(json.dumps(data, indent=2), encoding="utf-8")
    usage_instructions(root, selected)


def uninstall(root: Path, *, purge: bool = False) -> None:
    root = root.expanduser().absolute()
    manifest = load_manifest(root)
    for item in manifest["profiles"]:
        remove_block(profile_text(Path(item["path"])))
    for item in manifest["profiles"]:
        write_profile(Path(item["path"]), None)
    remove_desktop_shortcuts(root, manifest)
    for attempt in range(20):
        try:
            shutil.rmtree(root)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)
    if purge:
        (Path.home() / ".config/bravel/.env").unlink(missing_ok=True)
    print("  ✓ Bravel удалён. Откройте новый терминал.")
    if not purge:
        print("  Конфиг с API-ключом сохранён; --purge удаляет и его.")


def wait_for_parent(pid: int) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if handle:
            try:
                if kernel.WaitForSingleObject(handle, 30000) != 0:
                    raise RuntimeError("Bravel ещё запущен; повторите после его завершения")
            finally:
                kernel.CloseHandle(handle)
        return
    for _ in range(120):
        try:
            # A caller capturing output may reap the CLI only after this helper
            # closes its pipe. A zombie has already released executable files.
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists() and stat.read_text().rsplit(")", 1)[-1].strip().startswith("Z"):
                return
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise RuntimeError("Bravel ещё запущен")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Установить/удалить Bravel без прав администратора")
    parser.add_argument("action", nargs="?", choices=("install", "update", "uninstall"), default="install")
    parser.add_argument("--prefix", type=Path, default=default_root())
    parser.add_argument("--shell", choices=("auto", "powershell", "bash"), default="auto")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--source")
    parser.add_argument("--provider", choices=("openai", "gemini", "openrouter", "compatible"), help="Пропустить выбор провайдера в мастере")
    parser.add_argument("--no-configure", action="store_true")
    parser.add_argument("--no-profile", action="store_true")
    parser.add_argument("--desktop", action="store_true", help="Также установить интерфейс C# из GitHub Release")
    parser.add_argument("--desktop-source", type=Path, help="Папка локальной готовой сборки интерфейса")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--wait-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--self-remove", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.wait_pid:
            wait_for_parent(args.wait_pid)
        if args.action == "uninstall":
            uninstall(args.prefix, purge=args.purge)
        else:
            if args.action == "update":
                data = load_manifest(args.prefix)
                selected = [(item["shell"], Path(item["path"])) for item in data["profiles"]]
                source = args.source or data["source"]
            else:
                source = args.source
                shell = ("powershell" if os.name == "nt" else "bash") if args.shell == "auto" else args.shell
                selected = [] if args.no_profile else ([(shell, args.profile)] if args.profile else detect_profiles(shell))
            install(args.prefix, source=source, profiles=selected, provider=args.provider, configure=not args.no_configure and args.action == "install", desktop=args.desktop, desktop_source=args.desktop_source)
        if args.self_remove:
            Path(__file__).unlink(missing_ok=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"  ✕ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
