"""Stateful CLI agent. Every action requires a preview and approval."""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from threading import Event, Lock

from .apps import game_plan
from .programs import launch_plan
from .inventory import inventory_plan
from .policy import read_only
from .privacy import redact_data
from datetime import datetime, timezone
from .config import ConsoleError, Settings
from .context import context, shell_name
from .executor import dangerous, shell_argv
from .planner import Plan, Planner
from .ui import safe_text


class Agent:
    def __init__(self, *, cwd: Path | None = None, shell: str | None = None, settings: Settings | None = None):
        self.cwd = (cwd or Path.cwd()).resolve()
        self.shell = shell_name(shell)
        self.settings = settings
        self.history: list[dict] = []
        self.pending: tuple[str, Plan] | None = None
        self.last_result: dict | None = None
        self.rounds = 0
        self.mode = "ask"
        self._failed_commands: set[str] = set()
        self._cancel = Event()
        self._lock = Lock()
        self._command_directories: list[tempfile.TemporaryDirectory] = []

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            self.pending = None

    def reset(self) -> None:
        self.pending = None
        self.history.clear()
        self.last_result = None
        self.rounds = 0
        self._failed_commands.clear()

    def make_plan(self, prompt: str, *, continuation: bool = False) -> dict:
        with self._lock:
            self.pending = None
            self._cancel.clear()
        if continuation:
            if self.last_result is None:
                raise ConsoleError("Сначала выполните предложенный план.")
            if self.rounds >= 8:
                raise ConsoleError("Достигнут лимит 8 циклов. Начните новую задачу.")
            prompt = "Проанализируй результат последнего плана. Если задача закончена, ответь без команд; иначе предложи следующий шаг."
        else:
            prompt = prompt.strip()
            if not prompt or len(prompt) > 16000:
                raise ConsoleError("Запрос должен содержать от 1 до 16000 символов")
            self.rounds = 0
            self.last_result = None
            self._failed_commands.clear()
        self.history.append({"role": "user", "text": prompt})
        settings = self.settings or Settings.load()
        self.settings = settings
        self.history = redact_data(self.history, (settings.api_key,))
        ctx = context(self.shell)
        ctx.update(cwd=str(self.cwd), history=self.history[-12:], shell_variables_persist=False,
                   current_time=datetime.now(timezone.utc).isoformat())
        plan = None if continuation else inventory_plan(prompt) or game_plan(prompt, self.shell)
        plan = plan or (None if continuation else launch_plan(prompt, self.shell)) or Planner(settings).make_plan(prompt, ctx)
        if continuation and any(step.command.strip().casefold() in self._failed_commands for step in plan.steps):
            raise ConsoleError("Этот план повторяет уже неудачную команду без изменений. Уточните задачу; повтор автоматически не выполняется.")
        self.rounds += 1
        identifier = uuid.uuid4().hex
        with self._lock:
            if self._cancel.is_set():
                raise ConsoleError("Запрос отменён")
            self.pending = (identifier, plan) if plan.steps else None
        self.history.append({"role": "assistant", "summary": plan.summary,
                             "steps": [asdict(step) for step in plan.steps]})
        self.history = redact_data(self.history, (settings.api_key,))
        self.history = self.history[-12:]
        return {"plan_id": identifier, "summary": safe_text(plan.summary),
                "steps": [{**asdict(step), "dangerous": dangerous(step)} for step in plan.steps],
                "dangerous": any(dangerous(step) for step in plan.steps), "cwd": str(self.cwd)}

    def execute(self, identifier: str, *, approval: str) -> dict:
        with self._lock:
            if self.pending is None or identifier != self.pending[0]:
                raise ConsoleError("План устарел или уже выполнен. Запросите новый.")
            plan = self.pending[1]
            required = "RUN" if any(dangerous(step) for step in plan.steps) else "approve"
            if approval != required:
                raise ConsoleError("Выполнение не подтверждено")
            self.pending = None  # A plan is a one-use capability, not supplied executable text.
            self._cancel.clear()
        results = []
        for step in plan.steps:
            if step.check and not read_only(step.check, self.shell):
                raise ConsoleError("Проверка результата не подходит для текущей оболочки")
            if self._cancel.is_set():
                break
            result = self._run(step.command)
            if result["exit_code"] != 0:
                self._failed_commands.add(step.command.strip().casefold())
                output = result.get("output", "").casefold()
                result["needs_interaction"] = any(text in output for text in (
                    "error reading input in prompt", "0x8a150042", "cannot read from stdin", "requires an interactive terminal"))
            result["observed_at"] = datetime.now(timezone.utc).isoformat()
            if step.check and result["exit_code"] == 0 and not result["cancelled"] and not result["timed_out"]:
                verification = self._run(step.check)
                observed = verification["exit_code"] == 0
                if self.shell == "powershell":
                    observed = observed and verification["output"].strip().casefold() == "true"
                if not observed and not verification["cancelled"] and not verification["timed_out"]:
                    time.sleep(0.4)
                    verification = self._run(step.check)
                    observed = verification["exit_code"] == 0 and (self.shell != "powershell" or verification["output"].strip().casefold() == "true")
                result["verification"] = {"command": step.check, "status": "observed" if observed else "not_observed",
                                          "output": verification["output"], "exit_code": verification["exit_code"]}
            results.append(result)
            if result["exit_code"] != 0 or result["cancelled"] or result["timed_out"]:
                break
        self.last_result = {"results": results, "cancelled": self._cancel.is_set(), "cwd": str(self.cwd)}
        self.last_result = redact_data(self.last_result, (self.settings.api_key,) if self.settings else ())
        self.history.append({"role": "command_results", **self.last_result})
        return self.last_result

    def _run(self, command: str) -> dict:
        # Files avoid pipe deadlocks and bound RAM even if a command floods stdout.
        # Keep only a small tail for the model; every command remains explicitly approved.
        # Launched apps can inherit stdout and keep it open after the shell exits.
        # Do not kill the requested app or fail the task over a Windows file lock.
        # Retry cleanup on later commands, once the app releases the handle.
        for temporary in self._command_directories:
            temporary.cleanup()
        self._command_directories = [temporary for temporary in self._command_directories if Path(temporary.name).exists()]
        temporary = tempfile.TemporaryDirectory(prefix="bravel-command-", ignore_cleanup_errors=True)
        self._command_directories.append(temporary)
        with temporary as directory:
            cwd_file = Path(directory) / "cwd.txt"
            if self.shell == "powershell":
                destination = str(cwd_file).replace("'", "''")
                wrapped = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Console]::OutputEncoding; "
                           "$ErrorActionPreference='Stop'; $global:LASTEXITCODE=0;\n" + command +
                           "\n$ok=$?; $code=$global:LASTEXITCODE; " +
                           f"[IO.File]::WriteAllText('{destination}', (Get-Location).Path); " +
                           "if (-not $ok) { exit 1 }; if ($code -ne 0) { exit $code }")
            elif self.shell == "bash":
                import shlex
                wrapped = "{\n" + command + "\n}\nbravel_code=$?; pwd > " + shlex.quote(str(cwd_file)) + "; exit $bravel_code"
            else:
                wrapped = command
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            timed_out = False
            with (Path(directory) / "output.txt").open("w+b") as output:
                try:
                    process = subprocess.Popen(shell_argv(self.shell, wrapped), cwd=self.cwd, stdin=subprocess.DEVNULL,
                                               stdout=output, stderr=subprocess.STDOUT, **flags)
                except OSError as exc:
                    raise ConsoleError("Не удалось запустить команду") from exc
                deadline = time.monotonic() + 120
                try:
                    while process.poll() is None:
                        if self._cancel.wait(0.05) or time.monotonic() >= deadline or os.fstat(output.fileno()).st_size > 2_000_000:
                            timed_out = not self._cancel.is_set()
                            self._stop_process(process)
                            break
                    process.wait()
                except BaseException:
                    self._cancel.set()
                    self._stop_process(process)
                    raise
                output.seek(0, 2)
                size = output.tell()
                output.seek(max(0, size - 12000))
                raw = output.read(12000)
            encoding = "utf-8-sig" if self.shell != "cmd" or os.name != "nt" else "oem"
            text = safe_text(raw.decode(encoding, errors="replace"))
            if cwd_file.exists() and process.returncode == 0:
                candidate = Path(cwd_file.read_text(encoding="utf-8-sig", errors="replace").strip())
                if candidate.is_dir():
                    self.cwd = candidate.resolve()
            return {"command": command, "exit_code": process.returncode, "output": text,
                    "truncated": size > 12000, "timed_out": timed_out, "cancelled": self._cancel.is_set()}

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
