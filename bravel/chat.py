from __future__ import annotations

import sys
import json
from pathlib import Path

from .agent import Agent
from .apps import installed_steam_games
from .config import ConsoleError, Settings
from .executor import approve, shell_argv
from .planner import Plan, Step
from .ui import UI
from .programs import installed_applications
from .sessions import Sessions
from .privacy import redact_data
from .context import context
from .input import reader
from .tools import resolve_path


HELP = """Напиши задачу обычными словами или начни с #.
Каждый план нужно подтвердить; Enter и n отменяют, RUN разрешает повышенный риск.
После выполнения агент анализирует вывод и предлагает следующий шаг.

/help                 Подсказка
/clear                Очистить экран Bravel (также clear, cls)
/pwd                  Текущая папка агента
/cd <путь>            Сменить папку без запроса к API
/shell <имя>          powershell, cmd или bash
/status               Провайдер, модель и наличие ключа
/games                Установленные игры Steam
/apps                 Найденные приложения и пути
/continue             Проанализировать последний результат
/history              Показать историю текущего диалога
/new                  Очистить диалог, сохранив папку
/mode ask|preview|read-only  Подтверждение, просмотр или разрешённое чтение
/save <имя>           Сохранить диалог с маскировкой известных секретов
/load <имя>           Загрузить прошлый диалог без исполнения планов
/sessions             Список сохранённых диалогов
/delete <имя>         Удалить сохранённый диалог
/context              Данные контекста для следующего запроса
/privacy              Как используются данные
/exit                 Выйти
Ctrl+C                Остановить команду или отменить запрос

История и вывод команд используются в следующих запросах к выбранному API.
По умолчанию история только в памяти; /save и --session сохраняют её на диск.
Стрелки ↑/↓ — история, Tab — команды, Alt+Enter — новая строка.
exit и quit также закрывают Bravel."""


def run_task(agent: Agent, ui: UI, prompt: str, *, continuation: bool = False) -> int:
    with ui.busy("Анализирую результат…" if continuation else "Составляю план…"):
        result = agent.make_plan(prompt, continuation=continuation)
    while True:
        if calls := result.get("tools"):
            ui.panel("АГЕНТ", result["summary"])
            for call in calls:
                arguments = dict(call["arguments"])
                if "path" in arguments:
                    arguments["path"] = str(resolve_path(agent.cwd, arguments["path"]))
                if call["name"] in {"read_text", "write_text"}:
                    ui.panel("ЗАПИСЬ ФАЙЛА" if call["name"] == "write_text" else "ЧТЕНИЕ ФАЙЛА",
                             "Путь: " + arguments["path"] + ("\n\n" + arguments["content"] if "content" in arguments else ""))
            requires_approval = any(call["name"] in {"read_text", "write_text"} for call in calls)
            if agent.mode == "preview" or (requires_approval and not ui.confirm(dangerous=result["dangerous"])):
                agent.cancel()
                ui.note("Инструменты не выполнены.")
                return 0
            with ui.busy("Получаю данные…"):
                output = agent.execute(result["plan_id"], approval="RUN" if result["dangerous"] else "approve")
            for item in output["results"]:
                if item["exit_code"]:
                    ui.error(item["output"])
                elif item["tool"] == "write_text":
                    ui.panel("ФАЙЛ", item["output"])
                elif item["tool"] in {"games", "apps"}:
                    entries = item["data"][item["tool"]]
                    ui.panel("НАЙДЕНО", "\n".join(entry["name"] for entry in entries) or "В доступных местах ничего не найдено.")
                else:
                    text = item["data"].get("text", item["output"])
                    ui.panel("ПОЛУЧЕННЫЕ ДАННЫЕ", text[:6000] + ("\n[Показана часть данных]" if len(text) > 6000 else ""))
            with ui.busy("Анализирую полученные данные…"):
                result = agent.make_plan("", continuation=True)
            continue
        plan = Plan(result["summary"], tuple(Step(s["command"], s["explanation"], s["risk"], s.get("check", "")) for s in result["steps"]))
        steps = approve(plan, ui, mode=agent.mode, shell=agent.shell)
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
            if verification := item.get("verification"):
                ui.panel("ПРОВЕРКА РЕЗУЛЬТАТА", ("Условие подтверждено наблюдением." if verification["status"] == "observed" else
                         "Условие не подтверждено. Не считаем запуск или изменение проверенным.") +
                         "\n" + verification["command"] + "\n" + verification["output"])
        if output["cancelled"]:
            return 130
        if any(item.get("needs_interaction") for item in output["results"]):
            ui.note("Команда требует интерактивного ответа, а её stdin отключён. Цикл остановлен; соглашения автоматически не принимаются. /continue — запросить другой способ.")
            return 1
        with ui.busy("Анализирую вывод…"):
            result = agent.make_plan("", continuation=True)


def chat(agent: Agent, ui: UI, *, plain: bool = False, session: str | None = None) -> int:
    if not sys.stdin.isatty():
        raise ConsoleError("Диалог требует интерактивный терминал. Для одного запроса используйте bravel ask.")
    ui.banner()
    ui.note(f"Оболочка: {agent.shell}. Папка: {agent.cwd}")
    ui.note("Напиши задачу. /help — команды, /exit — выход. Вывод команд входит в контекст диалога.")
    sessions = Sessions()
    if session:
        if session in sessions.names():
            sessions.load(session, agent)
            ui.note("Загружена сохранённая история; старые результаты требуют новой проверки.")
        else:
            sessions.path(session)
        ui.note(f"Автосохранение диалога: {session}")
    read = reader(ui, plain=plain)
    while True:
        try:
            prompt = read().strip()
            if not prompt:
                continue
            if prompt.casefold() in {"/exit", "/quit", "exit", "quit"}:
                if session:
                    sessions.save(session, agent)
                return 0
            if prompt.casefold() in {"/clear", "clear", "cls"}:
                ui.clear()
                continue
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
                ui.panel("НАСТРОЙКИ", f"Провайдер: {settings.provider}\nМодель: {settings.model}\nКлюч: {'задан' if settings.api_key and settings.api_key != 'your-api-key' else 'не задан'}\nОболочка: {agent.shell}\nПапка: {agent.cwd}\nРежим: {agent.mode}")
                continue
            if prompt == "/games":
                games = installed_steam_games()
                listing = "\n".join(f"{game.name} · Steam {game.app_id}" for game in games) or "Установленные игры в доступных библиотеках Steam не найдены."
                ui.panel("STEAM", listing)
                agent.history.append({"role": "local_inventory", "text": listing})
                agent.history = agent.history[-12:]
                continue
            if prompt == "/apps":
                listing = "\n".join(f"{app.name} · {app.executable}" for app in installed_applications()) or "Приложения не найдены в стандартных местах."
                ui.panel("ПРИЛОЖЕНИЯ", listing)
                agent.history.append({"role": "local_inventory", "text": listing})
                agent.history = agent.history[-12:]
                continue
            if prompt.startswith("/mode "):
                mode = prompt[6:].strip()
                if mode not in {"ask", "preview", "read-only"}:
                    raise ConsoleError("Режим: ask, preview или read-only")
                agent.mode = mode
                agent.cancel()
                ui.note(f"Режим этой сессии: {mode}. Запуск приложений и изменения требуют подтверждения.")
                continue
            if prompt == "/sessions":
                ui.panel("ДИАЛОГИ", "\n".join(sessions.names()) or "Сохранённых диалогов нет.")
                continue
            if prompt.startswith(("/save ", "/load ", "/delete ")):
                action, name = prompt.split(" ", 1)
                name = name.strip()
                getattr(sessions, action[1:])(name, agent) if action != "/delete" else sessions.delete(name)
                ui.note(f"Диалог {name}: {'сохранён' if action == '/save' else 'загружен' if action == '/load' else 'удалён'}.")
                continue
            if prompt == "/context":
                settings = agent.settings or Settings.load()
                ctx = context(agent.shell)
                ctx.update(cwd=str(agent.cwd), history=agent.history[-12:], shell_variables_persist=False)
                ui.panel("КОНТЕКСТ ДЛЯ API", json.dumps(redact_data(ctx, (settings.api_key,)), ensure_ascii=False, indent=2))
                continue
            if prompt == "/privacy":
                ui.panel("ДАННЫЕ", "API получает запрос, ОС, оболочку, текущую папку и последние 12 записей диалога (включая вывод).\n"
                         "Известный API-ключ, типовые токены, пароли и приватные ключи маскируются. Маскировка не гарантирует распознавание всех секретов.\n"
                         "API-ключ используется только для авторизации у выбранного провайдера. /context показывает контекст.\n"
                         "История ввода в памяти. /save или --session сохраняет очищенную историю локально; /delete удаляет файл.")
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
            if session:
                sessions.save(session, agent)
        except EOFError:
            if session:
                sessions.save(session, agent)
            return 0
        except KeyboardInterrupt:
            agent.cancel()
            ui.note("Отменено. Можно написать другую задачу или /exit.")
        except ConsoleError as exc:
            ui.error(str(exc))
