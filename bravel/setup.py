from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import ConsoleError, PROVIDERS, Settings, config_path, template, read_env
from .ui import UI


def configure(provider: str | None, path: Path | None, ui: UI) -> int:
    if not sys.stdin.isatty():
        raise ConsoleError("Мастер требует терминал; используйте bravel init --provider ...")
    path = (path or config_path()).expanduser()
    current = Settings.load(path) if path.exists() else None
    ui.banner()
    if provider is None:
        ui.panel("ПРОВАЙДЕР", "1 · OpenAI\n2 · Gemini (Google AI Studio)\n3 · OpenRouter\n4 · Другой совместимый API / локальная модель")
        default = str(list(PROVIDERS).index(current.provider) + 1) if current else "1"
        answer = input(f"  Провайдер [{default}]: ").strip() or default
        if answer not in {"1", "2", "3", "4"}:
            raise ConsoleError("Выберите провайдера от 1 до 4")
        provider = list(PROVIDERS)[int(answer) - 1]
    base_url, model, _ = PROVIDERS[provider]
    if current and current.provider == provider:
        base_url, model = current.base_url, current.model
    url = input(f"  API URL [{base_url}]: ").strip() or base_url
    selected_model = input(f"  Модель [{model}]: ").strip() or model
    api_key = getpass.getpass("  API-ключ (скрыт; Enter = сохранить текущий): ").strip()
    if not api_key and current and current.provider == provider:
        api_key = current.api_key
    if not api_key and provider != "compatible":
        raise ConsoleError("API-ключ не задан. Конфиг не изменён.")
    if any(c in url + selected_model + api_key for c in "\r\n'"):
        raise ConsoleError("Недопустимый символ в настройках")
    content = template(provider).replace("AI_API_BASE_URL=" + PROVIDERS[provider][0], "AI_API_BASE_URL=" + url).replace("AI_MODEL=" + PROVIDERS[provider][1], "AI_MODEL=" + selected_model).replace("AI_API_KEY=your-api-key", "AI_API_KEY='" + api_key + "'")
    if path.exists():
        original = read_env(path)
        for key in ("AI_TIMEOUT", "AI_JSON_MODE", "AI_MAX_STEPS", "AI_COLOR"):
            if key in original:
                lines = content.splitlines()
                content = "\n".join(key + "=" + original[key] if line.startswith(key + "=") else line for line in lines) + "\n"
    if not api_key:
        content = content.replace("AI_REQUIRE_KEY=true", "AI_REQUIRE_KEY=false")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(content)
        temporary = Path(file.name)
    try:
        Settings.from_values(read_env(temporary))
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    ui.note(f"Сохранено: {path}. Ключ не отправлялся к API.")
    return 0


def managed_action(action: str, *, purge: bool, ui: UI) -> int:
    root = Path(sys.prefix).parent
    bundled_installer = Path(sys.prefix) / "share/bravel/install.py"
    installer = bundled_installer if bundled_installer.is_file() else root / "installer.py"
    if not installer.is_file() or not (root / "bravel-install.json").is_file():
        raise ConsoleError("Эта копия установлена через pip/pipx. Удаление: pipx uninstall bravel или python -m pip uninstall bravel. Для управления одной командой используйте install.py.")
    base_python = getattr(sys, "_base_executable", sys.executable)
    with tempfile.NamedTemporaryFile(prefix="bravel-maintenance-", suffix=".py", delete=False) as file:
        helper = Path(file.name)
    shutil.copy2(installer, helper)
    command = [base_python, str(helper), action, "--prefix", str(root), "--wait-pid", str(os.getpid()), "--self-remove", "--no-configure"]
    if purge:
        command.append("--purge")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    subprocess.Popen(command, env=environment, **kwargs)
    ui.note(f"{'Обновление' if action == 'update' else 'Удаление'} запущено. Откройте новый терминал после завершения.")
    return 0
