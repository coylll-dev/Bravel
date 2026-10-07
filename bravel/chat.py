from __future__ import annotations

import sys
from pathlib import Path

from .agent import Agent
from .apps import installed_steam_games
from .config import ConsoleError, Settings
from .executor import approve, shell_argv
from .planner import Plan, Step
from .ui import UI


HELP = """Напиши задачу обычными словами или начни с #.
Каждый план нужно подтвердить; Enter и n отменяют, RUN разрешает повышенный риск.
После выполнения агент анализирует вывод и предлагает следующий шаг.

/help                 Подсказка
/pwd                  Текущая папка агента
/cd <путь>            Сменить папку без запроса к API
/shell <имя>          powershell, cmd или bash
/status               Провайдер, модель и наличие ключа
/games                Установленные игры Steam
/continue             Проанализировать последний результат
/history              Показать историю текущего диалога
/new                  Очистить диалог, сохранив папку
/exit                 Выйти
Ctrl+C                Остановить команду или отменить запрос

История и вывод команд используются в следующих запросах к выбранному API.
Они хранятся только в памяти этой сессии."""


def run_task(agent: Agent, ui: UI, prompt: str, *, continuation: bool = False) -> int:
    ui.note("Анализирую результат…" if continuation else "Составляю план…")
    result = agent.make_plan(prompt, continuation=continuation)
    while True:
        plan = Plan(result["summary"], tuple(Step(s["command"], s["explanation"], s["risk"]) for s in result["steps"]))
        steps = approve(plan, ui)
        if not steps:
            agent.cancel()
            return 0
        output = agent.execute(result["plan_id"], approval="RUN" if result["dangerous"] else "approve")
        for item in output["results"]:
            text = item["output"] or "Команда завершилась без вывода."
            if item["truncated"]:
                text += "\n[Показана последняя часть вывода]"
            if item["timed_out"]:
                text += "\n[Остановлено: лимит времени или объёма вывода]"
            ui.panel(f"РЕЗУЛЬТАТ · код {item['exit_code']}", text)
        if output["cancelled"]:
            return 130
        ui.note("Проверяю результат…")
        result = agent.make_plan("", continuation=True)


def chat(agent: Agent, ui: UI) -> int:
    if not sys.stdin.isatty():
        raise ConsoleError("Диалог требует интерактивный терминал. Для одного запроса используйте bravel ask.")
    ui.banner()
    ui.note(f"Оболочка: {agent.shell}. Папка: {agent.cwd}")
    ui.note("Напиши задачу. /help — команды, /exit — выход. Вывод команд входит в контекст диалога.")
    while True:
        try:
            prompt = input(ui.paint("\n  bravel › ", "1;96")).strip()
            if not prompt:
                continue
            if prompt in {"/exit", "/quit"}:
                return 0
            if prompt == "/help":
                ui.panel("ДИАЛОГ", HELP)
                continue
            if prompt == "/new":
                agent.reset()
                ui.note("История очищена.")
                continue
            if prompt == "/pwd":
                ui.note(str(agent.cwd))
                continue
            if prompt == "/status":
                settings = agent.settings or Settings.load()
                ui.panel("НАСТРОЙКИ", f"Провайдер: {settings.provider}\nМодель: {settings.model}\nКлюч: {'задан' if settings.api_key and settings.api_key != 'your-api-key' else 'не задан'}\nОболочка: {agent.shell}\nПапка: {agent.cwd}")
                continue
            if prompt == "/games":
                games = installed_steam_games()
                listing = "\n".join(f"{game.name} · Steam {game.app_id}" for game in games) or "Установленные игры в доступных библиотеках Steam не найдены."
                ui.panel("STEAM", listing)
                agent.history.append({"role": "local_inventory", "text": listing})
                agent.history = agent.history[-12:]
                continue
            if prompt.startswith("/shell "):
                shell = prompt[7:].strip()
                if shell not in {"powershell", "cmd", "bash"}:
                    raise ConsoleError("Оболочка: powershell, cmd или bash")
                shell_argv(shell, "echo bravel")
                agent.shell = shell
                agent.cancel()
                ui.note(f"Оболочка: {shell}")
                continue
            if prompt.startswith("/cd "):
                path = Path(prompt[4:].strip().strip('"').strip("'")).expanduser()
                path = (path if path.is_absolute() else agent.cwd / path).resolve()
                if not path.is_dir():
                    raise ConsoleError("Папка не найдена")
                agent.cwd = path
                agent.cancel()
                ui.note(f"Папка: {agent.cwd}")
                continue
            if prompt == "/history":
                for item in agent.history:
                    if item["role"] == "user":
                        ui.panel("ТЫ", item["text"])
                    elif item["role"] == "assistant":
                        ui.panel("BRAVEL", item["summary"])
                    elif item["role"] == "command_results":
                        for result in item["results"]:
                            ui.panel(f"ВЫВОД · код {result['exit_code']}", result.get("output", ""))
                    else:
                        ui.panel("ЛОКАЛЬНО", item.get("text", ""))
                continue
            if prompt.startswith("/") and prompt != "/continue":
                raise ConsoleError("Неизвестная команда диалога. /help — подсказка.")
            run_task(agent, ui, prompt.removeprefix("#").strip(), continuation=prompt == "/continue")
        except EOFError:
            return 0
        except KeyboardInterrupt:
            agent.cancel()
            ui.note("Отменено. Можно написать другую задачу или /exit.")
        except ConsoleError as exc:
            ui.error(str(exc))
