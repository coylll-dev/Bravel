from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

from . import __version__
from .apps import game_plan
from .programs import launch_plan
from .config import ConsoleError, Settings, PROVIDERS, config_path, template
from .context import context, shell_name
from .executor import approve, execute, script_for
from .planner import Planner, local_fix
from .ui import UI
from .setup import configure, managed_action
from .agent import Agent


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
    cmd = sub.add_parser("doctor", help="Проверить настройки, PATH и подключение оболочки")
    cmd.add_argument("--api", action="store_true", help="Также проверить API и модель коротким запросом")
    sub.add_parser("version", help="Показать версию")
    cmd = sub.add_parser("init", help="Создать шаблон конфигурации")
    cmd.add_argument("--path", type=Path, help="Путь к новому .env")
    cmd.add_argument("--provider", choices=tuple(PROVIDERS), default="openai")
    cmd = sub.add_parser("configure", help="Мастер настройки API")
    cmd.add_argument("--provider", choices=tuple(PROVIDERS))
    cmd.add_argument("--path", type=Path)
    sub.add_parser("update", help="Обновить управляемую установку")
    sub.add_parser("rollback", help="Вернуться к снимку перед последним обновлением")
    cmd = sub.add_parser("uninstall", help="Удалить Bravel и подключение")
    cmd.add_argument("--purge", action="store_true", help="Также удалить стандартный .env")
    cmd = sub.add_parser("integration", help="Вывести путь к скрипту подключения")
    cmd.add_argument("shell", choices=("powershell", "bash"))
    sub.add_parser("demo", help="Показать интерфейс без API и запуска команд")
    cmd = sub.add_parser("chat", help="Интерактивный диалог с агентом")
    cmd.add_argument("--shell", choices=("powershell", "bash", "cmd"))
    cmd.add_argument("--plain", action="store_true", help="Обычный ввод без редактора строк")
    cmd.add_argument("--session", help="Именованный диалог с загрузкой и автосохранением")
    cmd.add_argument("--mode", choices=("ask", "preview", "read-only"), default="ask")
    cmd = sub.add_parser("agent", help="Диалог с анализом результатов команд")
    cmd.add_argument("--shell", choices=("powershell", "bash", "cmd"))
    cmd.add_argument("--dry-run", action="store_true", help="Показать план без выполнения")
    cmd.add_argument("text", nargs="*")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["chat"]
    if arguments[0].startswith("--") and arguments[0] not in {"--help", "--version"}:
        suggestion = difflib.get_close_matches(arguments[0], ["--help", "--version"], n=1, cutoff=0.7)
        if suggestion:
            UI(stream=sys.stderr).error(f"Неизвестный параметр {arguments[0]}. Возможно, вы имели в виду {suggestion[0]}?")
            raise SystemExit(2)
    actions = {"ask", "fix", "doctor", "version", "init", "configure", "update", "rollback", "uninstall", "integration", "demo", "chat", "agent"}
    if arguments and not arguments[0].startswith("-") and arguments[0] not in actions:
        arguments.insert(0, "ask")
    args = parser().parse_args(arguments)
    ui = UI(stream=None if getattr(args, "emit_command", False) else sys.stdout, terminal=sys.stdin.isatty())
    try:
        if args.action == "version":
            print(__version__)
            return 0
        if args.action in {"agent", "chat"}:
            from .chat import chat, run_task
            agent = Agent(cwd=Path.cwd(), shell=args.shell)
            agent.mode = getattr(args, "mode", "preview" if getattr(args, "dry_run", False) else "ask")
            try:
                if args.action == "agent" and args.text:
                    ui.banner()
                return chat(agent, ui, plain=getattr(args, "plain", False), session=getattr(args, "session", None)) if args.action == "chat" or not args.text else run_task(agent, ui, " ".join(args.text))
            except KeyboardInterrupt:
                agent.cancel()
                ui.note("Остановлено пользователем.")
                return 130
        if args.action == "configure":
            return configure(args.provider, args.path, ui)
        if args.action in {"update", "rollback", "uninstall"}:
            return managed_action(args.action, purge=getattr(args, "purge", False), ui=ui)
        if args.action == "integration":
            suffix = "ps1" if args.shell == "powershell" else "bash"
            print(Path(__file__).parent / "integrations" / f"bravel.{suffix}")
            return 0
        if args.action == "init":
            path = (args.path or config_path()).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as file:
                    file.write(template(args.provider))
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
        emit_mode = bool(getattr(args, "emit_command", False))
        ui = UI(settings.color, None if emit_mode else sys.stdout, terminal=not emit_mode and sys.stdin.isatty())
        if args.action == "doctor":
            from .diagnostics import doctor
            return doctor(settings, ui, api=args.api)
        shell = shell_name(args.shell)
        prompt = " ".join(args.text).strip()
        if args.action == "ask":
            prompt = prompt.removeprefix("#").strip()
        if not prompt or len(prompt) > 16000:
            raise ConsoleError("Запрос должен содержать от 1 до 16000 символов")
        if args.action == "ask" and not args.emit_command and not args.command_file:
            from .chat import run_task
            agent = Agent(cwd=Path.cwd(), shell=shell, settings=settings)
            agent.mode = "preview" if args.dry_run else "ask"
            ui.banner()
            return run_task(agent, ui, prompt)
        ui.banner()
        ctx = context(shell)
        plan = local_fix(prompt, ctx) if args.action == "fix" else game_plan(prompt, shell) or launch_plan(prompt, shell)
        if plan:
            ui.note("Результат найден локально — запрос к API не нужен.")
        else:
            ui.note("Составляю план…")
            plan = Planner(settings).make_plan(prompt, ctx, failed=args.action == "fix")
        if args.dry_run:
            approve(plan, ui, mode="preview", shell=shell)
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
