"""Route inventory questions to existing local discovery instead of guessed commands."""
from __future__ import annotations

import re
from .apps import installed_steam_games
from .programs import installed_applications
from .planner import Plan


def listing(kind: str) -> str:
    if kind == "games":
        games = installed_steam_games()
        return "Установленные игры в доступных библиотеках Steam:\n" + (
            "\n".join(f"{game.name} · Steam {game.app_id}" for game in games) or "Не найдены. Игры вне Steam этот список не охватывает.")
    apps = installed_applications()
    return "Приложения, найденные в PATH и стандартных местах установки:\n" + (
        "\n".join(f"{app.name} · {app.executable}" for app in apps) or "Не найдены. Это не полный список всех установленных пакетов.")


def inventory_plan(prompt: str) -> Plan | None:
    if re.search(r"\b(?:запусти|открой|удали|установи|обнови|найди файл|launch|open|remove|install|update)\b", prompt, re.I):
        return None
    question = re.search(r"установлен|список|какие|покажи|вывод|вывести|посмотреть|\blist\b|\binstalled\b", prompt, re.I)
    if not question:
        return None
    kind = "games" if re.search(r"\bигр[а-я]*\b|\bgames\b|\bsteam\b", prompt, re.I) else (
        "apps" if re.search(r"приложен|программ|\bapps\b|\bapplications\b|\bprograms\b", prompt, re.I) else None)
    if kind is None:
        return None
    if re.search(r"\bкоманд[а-я]*\b|\bcommand\b", prompt, re.I):
        return Plan(f"Команда Bravel: bravel {kind}\nВ диалоге: /{kind}\nСписок читается локально без API и winget. " +
                    ("Поиск игр охватывает доступные библиотеки Steam." if kind == "games" else "Поиск приложений ограничен PATH и стандартными местами установки."), ())
    return Plan(listing(kind), ())
