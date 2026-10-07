from __future__ import annotations

import re
import shutil
import subprocess

from .config import ConsoleError
from .planner import Plan, Step
from .ui import UI


RISK_PATTERNS = (
    r"\b(rm|rmdir|del|erase|remove-item|format|mkfs|diskpart|dd|shred)\b",
    r"\b(sudo|su|runas|chmod|chown|set-executionpolicy|reg|shutdown|reboot)\b",
    r"\b(install|uninstall|upgrade|iex|invoke-expression|invoke-webrequest|curl|wget)\b",
    r"\bgit\s+(reset|clean|push)\b",
    r">|\b(set-content|out-file|tee-object)\b",
    r"\b(python[\d.]*|node|perl|ruby|bash|sh|pwsh|powershell|cmd)\b\s+.*(-c|-command|/c|-encodedcommand|-enc)\b",
)


def dangerous(step: Step) -> bool:
    # Heuristic only; the displayed command and mandatory approval are the boundary.
    return step.risk == "high" or any(re.search(pattern, step.command, re.I) for pattern in RISK_PATTERNS)


def shell_argv(shell: str, command: str) -> list[str]:
    if shell == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            raise ConsoleError("PowerShell не найден")
        return [executable, "-NoLogo", "-NoProfile", "-Command", command]
    executable = shutil.which(shell)
    if not executable:
        raise ConsoleError(f"Оболочка {shell} не найдена")
    if shell == "cmd":
        return [executable, "/d", "/s", "/c", command]
    return [executable, "-c", command]


def approve(plan: Plan, ui: UI) -> tuple[Step, ...]:
    ui.panel("ПЛАН", plan.summary)
    for index, step in enumerate(plan.steps, 1):
        high = dangerous(step)
        ui.panel(f"{index}/{len(plan.steps)} · {'ПОВЫШЕННЫЙ РИСК' if high else step.risk.upper()}", step.command + "\n\n" + step.explanation, "93" if high else "96")
    if not plan.steps:
        return ()
    if len(plan.steps) > 1:
        ui.note("Подтверждение разрешает весь показанный план. Выполнение остановится при ошибке.")
    if not ui.confirm(dangerous=any(dangerous(step) for step in plan.steps)):
        ui.note("Отменено. Команды не выполнены.")
        return ()
    return plan.steps


def script_for(steps: tuple[Step, ...], shell: str) -> str:
    # This is returned to the parent shell ONLY after approval; no wrappers with
    # additional commands may be smuggled into the preview by a model response.
    if shell == "powershell":
        parts = []
        for step in steps:
            parts.append(step.command)
            parts.append("if (-not $?) { throw 'Bravel: выполнение остановлено после ошибки' }")
        return "\n".join(parts)
    if shell == "cmd":
        return " && ".join(f"({step.command})" for step in steps)
    return " &&\n".join("{\n" + step.command + "\n}" for step in steps)


def execute(steps: tuple[Step, ...], shell: str, ui: UI) -> int:
    if not steps:
        return 0
    ui.note("Запуск подтверждённого плана…")
    try:
        code = subprocess.run(shell_argv(shell, script_for(steps, shell)), check=False).returncode
        ui.note(f"Команды завершились с кодом {code}.")
        return code
    except OSError as exc:
        raise ConsoleError("Не удалось запустить оболочку") from exc
