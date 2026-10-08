"""Explicit, named, redacted chat snapshots. Never persist executable capabilities."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .config import ConsoleError, config_path, Settings
from .privacy import redact_data


class Sessions:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or config_path().parent / "sessions"

    def path(self, name: str) -> Path:
        if not re.fullmatch(r"[\w-]{1,64}", name) or name in {".", ".."}:
            raise ConsoleError("Имя диалога: 1–64 буквы, цифры, дефис или подчёркивание")
        path = self.directory / (name + ".json")
        if self.directory.is_symlink() or path.is_symlink():
            raise ConsoleError("Ссылки в папке диалогов не поддерживаются")
        return path

    def save(self, name: str, agent) -> None:
        path = self.path(name)
        settings = agent.settings or Settings.load()
        secrets = (settings.api_key,)
        data = redact_data({"app": "bravel-chat", "format": 1, "cwd": str(agent.cwd),
                            "shell": agent.shell, "history": agent.history[-12:]}, secrets)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if len(content.encode("utf-8")) > 1_000_000:
            raise ConsoleError("Диалог слишком большой для сохранения")
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile("w", dir=self.directory, encoding="utf-8", delete=False) as file:
                file.write(content)
                temporary = Path(file.name)
            try:
                if os.name != "nt":
                    temporary.chmod(0o600)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise ConsoleError("Не удалось сохранить диалог") from exc

    def load(self, name: str, agent) -> None:
        path = self.path(name)
        try:
            if path.stat().st_size > 1_000_000:
                raise ValueError()
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("app") != "bravel-chat" or data.get("format") != 1 or not isinstance(data.get("history"), list):
                raise ValueError()
            if data.get("shell") not in {"cmd", "powershell", "bash"} or not isinstance(data.get("cwd"), str):
                raise ValueError()
            history = data["history"][-12:]
            for item in history:
                if not isinstance(item, dict) or not isinstance(item.get("role"), str):
                    raise ValueError()
                if item["role"] == "user" and not isinstance(item.get("text"), str):
                    raise ValueError()
                if item["role"] == "assistant" and not isinstance(item.get("summary"), str):
                    raise ValueError()
                if item["role"] == "command_results" and (not isinstance(item.get("results"), list) or
                    any(not isinstance(result, dict) or not isinstance(result.get("exit_code"), int) or
                        not isinstance(result.get("output", ""), str) for result in item["results"])):
                    raise ValueError()
                item["historical"] = True
            cwd = Path(data["cwd"])
            agent.reset()
            agent.history = redact_data(history, (agent.settings.api_key,) if agent.settings else ())
            # Use the current machine's shell and keep cwd if the old folder vanished.
            if cwd.is_dir():
                agent.cwd = cwd.resolve()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ConsoleError("Диалог не найден или имеет неверный формат") from exc

    def names(self) -> list[str]:
        if self.directory.is_symlink():
            raise ConsoleError("Ссылки в папке диалогов не поддерживаются")
        return sorted(path.stem for path in self.directory.glob("*.json") if not path.is_symlink())

    def delete(self, name: str) -> None:
        path = self.path(name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("app") != "bravel-chat":
                raise ValueError()
            path.unlink()
        except (OSError, ValueError, AttributeError) as exc:
            raise ConsoleError("Не удалось удалить диалог Bravel") from exc
