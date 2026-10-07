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
        self._cancel = Event()
        self._lock = Lock()

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            self.pending = None

    def reset(self) -> None:
        self.pending = None
        self.history.clear()
        self.last_result = None
        self.rounds = 0

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
        self.history.append({"role": "user", "text": prompt})
        settings = self.settings or Settings.load()
        ctx = context(self.shell)
        ctx.update(cwd=str(self.cwd), history=self.history[-12:])
        plan = None if continuation else game_plan(prompt, self.shell)
        plan = plan or Planner(settings).make_plan(prompt, ctx)
        self.rounds += 1
        identifier = uuid.uuid4().hex
        with self._lock:
            if self._cancel.is_set():
                raise ConsoleError("Запрос отменён")
            self.pending = (identifier, plan) if plan.steps else None
        self.history.append({"role": "assistant", "summary": plan.summary,
                             "steps": [asdict(step) for step in plan.steps]})
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
            if self._cancel.is_set():
                break
            result = self._run(step.command)
            results.append(result)
            if result["exit_code"] != 0 or result["cancelled"] or result["timed_out"]:
                break
        self.last_result = {"results": results, "cancelled": self._cancel.is_set(), "cwd": str(self.cwd)}
        self.history.append({"role": "command_results", **self.last_result})
        return self.last_result

    def _run(self, command: str) -> dict:
        # Files avoid pipe deadlocks and bound RAM even if a command floods stdout.
        # Keep only a small tail for the model; every command remains explicitly approved.
        with tempfile.TemporaryDirectory(prefix="bravel-command-") as directory:
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
