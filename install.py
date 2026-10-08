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
import tempfile
import venv
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


def path_entries(value: str, directory: Path, *, remove: bool = False) -> tuple[str, bool]:
    """Edit only the matching PATH entry; retain spelling and empty entries."""
    normalize = lambda item: os.path.normcase(os.path.normpath(os.path.expandvars(item.strip().strip('"'))))
    target = normalize(str(directory))
    entries = value.split(";") if value else []
    present = any(normalize(item) == target for item in entries)
    if remove:
        return ";".join(item for item in entries if normalize(item) != target), present
    if present:
        return value, False
    return value + (";" if value else "") + str(directory), True


def windows_user_path(directory: Path, *, remove: bool = False) -> bool:
    """Persist current user's PATH, retaining its registry type and other entries."""
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            value, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            raise RuntimeError("Неизвестный тип пользовательского PATH; он не изменён")
        updated, changed = path_entries(value, directory, remove=remove)
        if changed:
            winreg.SetValueEx(key, "Path", 0, kind, updated)
    if changed:
        import ctypes
        from ctypes import wintypes
        send = ctypes.WinDLL("user32", use_last_error=True).SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = wintypes.LPARAM
        result = ctypes.c_size_t()
        send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(result))
    return changed


def usage_instructions(root: Path, profiles: list[tuple[str, Path]]) -> None:
    shells = {shell for shell, _ in profiles}
    print("  Диалог агента: bravel chat. Одна задача: bravel agent \"проверь сеть\".")
    if "powershell" in shells:
        print("  Автоподключение к оболочке: откройте новый PowerShell (из CMD: powershell).")
        print("  Автоматические # запросы и исправления работают в PowerShell; CMD поддерживает только прямой CLI.")
        if os.name == "nt":
            for name in ("powershell", "pwsh"):
                executable = shutil.which(name)
                if executable:
                    result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"], capture_output=True, text=True)
                    if result.returncode == 0 and result.stdout.strip() in {"Restricted", "AllSigned"}:
                        print(f"  ! {name}: политика {result.stdout.strip()} может блокировать профиль Bravel.")
                        print("  Для CLI и bravel chat менять политику не нужно; она не изменялась установщиком.")
    if "bash" in shells:
        print("  Откройте новую интерактивную сессию Bash.")
    if not shells:
        print("  Подключение оболочки отключено; запускайте CLI из папки venv/Scripts или venv/bin установки.")
    print('  Проверка: bravel doctor. Запрос: ai "покажи сетевые подключения" или # помоги')
    print("  Управление: bravel configure, bravel update, bravel uninstall")
    if os.name == "nt":
        executable = root / "venv/Scripts/bravel.exe"
        print("  Для нового PATH закройте все окна терминала и откройте CMD из меню Пуск.")
        print(f'  CLI из CMD: "{executable}" ask --shell cmd "помоги"')
        print(f'  Короткая команда на текущую сессию CMD: doskey bravel="{executable}" $*')


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


def remove_legacy_desktop(root: Path, previous: dict | None) -> None:
    """Migrate old GUI installs to CLI without touching the engine or config."""
    desktop = root / "desktop"
    if desktop.exists():
        if not previous or not (previous.get("desktop") or (desktop / "Bravel.Desktop.exe").is_file() or (desktop / "Bravel.Desktop").is_file()):
            return
        if desktop.is_symlink() or root.resolve() not in desktop.resolve().parents:
            raise RuntimeError("Небезопасный путь старого интерфейса; удаление отменено")
        remove_desktop_shortcuts(root, previous or {})
        try:
            shutil.rmtree(desktop)
        except PermissionError as exc:
            raise RuntimeError("Закройте старое окно Bravel и повторите обновление") from exc
        print("  ✓ Старый интерфейс удалён; остаётся CLI.")
    elif previous:
        remove_desktop_shortcuts(root, previous)


def owned_child(root: Path, path: Path) -> Path:
    if path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise RuntimeError("Небезопасный путь снимка Bravel")
    return path


def snapshot_data(root: Path) -> dict:
    backup = owned_child(root, root / "rollback")
    data = json.loads((backup / "snapshot.json").read_text(encoding="utf-8"))
    if data.get("app") != "bravel-snapshot" or data.get("root") != str(root.resolve()):
        raise RuntimeError("Папка rollback не принадлежит Bravel; она не изменена")
    return data


def make_snapshot(root: Path, manifest: dict, version: str) -> None:
    backup = owned_child(root, root / "rollback")
    if backup.exists():
        snapshot_data(root)
    stage = Path(tempfile.mkdtemp(prefix="bravel-snapshot-", dir=root))
    try:
        (stage / "snapshot.json").write_text(json.dumps({"app": "bravel-snapshot", "root": str(root.resolve()),
                                                       "version": version, "manifest": manifest}), encoding="utf-8")
        shutil.copytree(owned_child(root, root / "venv"), stage / "venv", symlinks=True)
        for name in ("hooks",):
            if (root / name).exists():
                shutil.copytree(owned_child(root, root / name), stage / name, symlinks=True)
        if (root / "installer.py").is_file():
            shutil.copy2(root / "installer.py", stage / "installer.py")
        if backup.exists():
            shutil.rmtree(owned_child(root, backup))
        stage.rename(backup)
        print(f"  ✓ Снимок для отката: {version}. Команда: bravel rollback")
    finally:
        if stage.exists():
            shutil.rmtree(owned_child(root, stage))


def rollback(root: Path) -> None:
    root = root.expanduser().absolute()
    load_manifest(root)
    data = snapshot_data(root)
    backup = owned_child(root, root / "rollback")
    source = owned_child(root, backup / "venv")
    if not source.is_dir():
        raise RuntimeError("Снимок не содержит окружение Bravel")
    environment = owned_child(root, root / "venv")
    displaced = Path(tempfile.mkdtemp(prefix="bravel-replaced-", dir=root))
    displaced.rmdir()
    try:
        environment.rename(displaced)
        try:
            source.rename(environment)
        except OSError:
            displaced.rename(environment)
            raise
        for name in ("hooks",):
            destination = owned_child(root, root / name)
            saved = owned_child(root, backup / name)
            if saved.exists():
                if destination.exists():
                    shutil.rmtree(destination)
                saved.rename(destination)
        if (backup / "installer.py").is_file():
            shutil.copy2(backup / "installer.py", root / "installer.py")
        (root / "bravel-install.json").write_text(json.dumps(data["manifest"], indent=2), encoding="utf-8")
        print(f"  ✓ Откат завершён: Bravel {data['version']}. API-конфиг сохранён.")
    finally:
        if displaced.exists() and environment.exists():
            shutil.rmtree(owned_child(root, displaced))
    shutil.rmtree(owned_child(root, backup))


def install(root: Path, *, source: str | None = None, profiles: list[tuple[str, Path]] | None = None, provider: str | None = None, configure: bool = True, register_path: bool = True) -> None:
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
    environment = root / "venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if previous and python.is_file():
        version = subprocess.run([str(python), "-I", "-m", "bravel", "--version"], check=True, capture_output=True, encoding="utf-8").stdout.strip()
        print(f"  · Текущая версия: {version}")
        make_snapshot(root, previous, version)
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"app": "bravel", "root": str(root.resolve()), "profiles": [{"shell": shell, "path": str(path.absolute())} for shell, path in selected], "source": source or SOURCE_URL,
                "register_path": register_path, "path_added": bool(previous and previous.get("path_added"))}
    marker.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if not environment.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    local = Path(__file__).resolve().parent
    package_source = source or (str(local) if (local / "pyproject.toml").exists() else SOURCE_URL)
    pip_environment = os.environ.copy()
    pip_environment["PYTHONUTF8"] = "1"
    try:
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", package_source], check=True, env=pip_environment)
    except subprocess.CalledProcessError:
        if previous and (root / "rollback/snapshot.json").is_file():
            rollback(root)
        raise
    hooks = root / "hooks"
    hooks.mkdir(exist_ok=True)
    for suffix, shell in (("ps1", "powershell"), ("bash", "bash")):
        result = subprocess.run([str(python), "-I", "-m", "bravel", "integration", shell], check=True, capture_output=True, encoding="utf-8")
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
    if os.name == "nt" and register_path:
        added = windows_user_path(environment / "Scripts")
        manifest["path_added"] = manifest["path_added"] or added
        marker.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print("  ✓ Bravel доступен через пользовательский PATH в CMD и PowerShell.")
    print(f"  ◆ Bravel установлен: {root}")
    if configure:
        subprocess.run(configure_command(python, provider), check=True)
    remove_legacy_desktop(root, previous)
    version = subprocess.run([str(python), "-I", "-m", "bravel", "--version"], check=True, capture_output=True, encoding="utf-8").stdout.strip()
    print(f"  ✓ Установка завершена: Bravel {version}")
    usage_instructions(root, selected)


def uninstall(root: Path, *, purge: bool = False) -> None:
    root = root.expanduser().absolute()
    manifest = load_manifest(root)
    for item in manifest["profiles"]:
        remove_block(profile_text(Path(item["path"])))
    for item in manifest["profiles"]:
        write_profile(Path(item["path"]), None)
    remove_desktop_shortcuts(root, manifest)
    if os.name == "nt" and manifest.get("path_added"):
        windows_user_path(root / "venv/Scripts", remove=True)
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
    parser.add_argument("action", nargs="?", choices=("install", "update", "rollback", "uninstall"), default="install")
    parser.add_argument("--prefix", type=Path, default=default_root())
    parser.add_argument("--shell", choices=("auto", "powershell", "bash"), default="auto")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--source")
    parser.add_argument("--provider", choices=("openai", "gemini", "openrouter", "compatible"), help="Пропустить выбор провайдера в мастере")
    parser.add_argument("--no-configure", action="store_true")
    parser.add_argument("--no-profile", action="store_true")
    parser.add_argument("--no-path", action="store_true", help="Не добавлять CLI в пользовательский PATH Windows")
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("--wait-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--self-remove", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.wait_pid:
            wait_for_parent(args.wait_pid)
        if args.action == "uninstall":
            uninstall(args.prefix, purge=args.purge)
        elif args.action == "rollback":
            rollback(args.prefix)
        else:
            if args.action == "update":
                data = load_manifest(args.prefix)
                selected = [(item["shell"], Path(item["path"])) for item in data["profiles"]]
                source = args.source or data["source"]
            else:
                source = args.source
                shell = ("powershell" if os.name == "nt" else "bash") if args.shell == "auto" else args.shell
                selected = [] if args.no_profile else ([(shell, args.profile)] if args.profile else detect_profiles(shell))
            register_path = not args.no_path and (args.action != "update" or data.get("register_path", True))
            install(args.prefix, source=source, profiles=selected, provider=args.provider, configure=not args.no_configure and args.action == "install", register_path=register_path)
        if args.self_remove:
            Path(__file__).unlink(missing_ok=True)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"  ✕ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
