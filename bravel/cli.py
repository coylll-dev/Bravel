from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .apps import game_plan
from .config import ConsoleError, Settings, config_path
from .context import context, shell_name
from .executor import approve, execute, script_for
from .planner import Planner, local_fix
from .ui import UI


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bravel", description="ИИ-помощник внутри привычной консоли")
    result.add_argument("--version", action="version", version=__version__)
    sub = result.add_subparsers(dest="action", required=True)
    for name, help_text in (("ask", "Запрос на естественном языке"), ("fix", "Исправить неизвестную команду")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--shell", choices=("powershell", "bash", "cmd"))
        cmd.add_argument("--dry-run", action="store_true", help="Только показать план")
        cmd.add_argument("--emit-command", action="store_true", help="Для интеграций: вернуть подтверждённый скрипт в stdout")
        cmd.add_argument("--command-file", type=Path, help="Для PowerShell: записать подтверждённый скрипт в файл")
        cmd.add_argument("text", nargs="+", help="Запрос или неизвестная команда")
    sub.add_parser("doctor", help="Проверить локальные настройки без запроса к API")
    cmd = sub.add_parser("init", help="Создать шаблон конфигурации")
    cmd.add_argument("--path", type=Path, help="Путь к новому .env")
    cmd = sub.add_parser("integration", help="Вывести путь к скрипту подключения")
    cmd.add_argument("shell", choices=("powershell", "bash"))
    sub.add_parser("demo", help="Показать интерфейс без API и запуска команд")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    ui = UI()
    try:
        if args.action == "integration":
            suffix = "ps1" if args.shell == "powershell" else "bash"
            print(Path(__file__).parent / "integrations" / f"bravel.{suffix}")
            return 0
        if args.action == "init":
            path = (args.path or config_path()).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as file:
                    file.write("AI_API_BASE_URL=https://api.openai.com/v1\nAI_API_KEY=your-api-key\nAI_MODEL=gpt-4o-mini\nAI_TIMEOUT=45\nAI_JSON_MODE=true\nAI_MAX_STEPS=5\nAI_REQUIRE_KEY=true\nAI_COLOR=auto\n")
                if os.name != "nt":
                    path.chmod(0o600)
            except FileExistsError:
                raise ConsoleError(f"Конфиг уже существует: {path}. Файл не изменён.")
            ui.note(f"Конфиг создан: {path}")
            return 0
        if args.action == "demo":
            ui.banner()
            ui.panel("ЗАПРОС", "# команда для вывода сетей")
            ui.panel("1/1 · LOW", "netsh wlan show networks\n\nПоказать доступные Wi-Fi сети в Windows.")
            ui.note("Демо: команды не запускаются и API не вызывается.")
            ui.write(ui.paint("  Выполнить? [Y/n] (Enter = n): ", "1;96"))
            return 0
        settings = Settings.load()
        command_file_mode = bool(getattr(args, "command_file", None))
        ui = UI(settings.color, sys.stdout if command_file_mode else None, terminal=command_file_mode and sys.stdin.isatty())
        if args.action == "doctor":
            ui.banner()
            ui.panel("НАСТРОЙКИ", f"Конфиг: {config_path()}\nAPI: {settings.base_url}\nМодель: {settings.model}\nКлюч: {'задан' if settings.api_key and settings.api_key != 'your-api-key' else 'не задан'}\nPython: {sys.version.split()[0]}\nShell: {shell_name()}\nКоманды: {', '.join(context(shell_name())['available_commands'])}")
            settings.validate_key()
            ui.note("Локальные настройки корректны. Соединение с API не проверялось.")
            return 0
        shell = shell_name(args.shell)
        prompt = " ".join(args.text).strip()
        if args.action == "ask":
            prompt = prompt.removeprefix("#").strip()
        if not prompt or len(prompt) > 16000:
            raise ConsoleError("Запрос должен содержать от 1 до 16000 символов")
        ui.banner()
        ctx = context(shell)
        plan = local_fix(prompt, ctx) if args.action == "fix" else game_plan(prompt, shell)
        if plan:
            ui.note("Результат найден локально — запрос к API не нужен.")
        else:
            ui.note("Составляю план…")
            plan = Planner(settings).make_plan(prompt, ctx, failed=args.action == "fix")
        if args.dry_run:
            ui.panel("ПЛАН · ПРОСМОТР", plan.summary)
            for step in plan.steps:
                ui.panel(step.risk.upper(), step.command + "\n\n" + step.explanation)
            ui.note("Режим просмотра: команды не выполнены.")
            return 0
        steps = approve(plan, ui)
        if args.command_file:
            try:
                args.command_file.write_text(script_for(steps, shell) if steps else "", encoding="utf-8")
            except OSError as exc:
                raise ConsoleError("Не удалось записать подтверждённый скрипт") from exc
            return 0
        if args.emit_command:
            if steps:
                print(script_for(steps, shell))
            return 0
        return execute(steps, shell, ui)
    except ConsoleError as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        ui.note("Прервано.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
