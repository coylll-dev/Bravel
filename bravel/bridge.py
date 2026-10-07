"""Private stdio JSON RPC for the C# desktop. Never opens a network listener."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from . import __version__
from .agent import Agent
from .config import ConsoleError, PROVIDERS, Settings, config_path, read_env, template


def status(agent: Agent) -> dict:
    settings = Settings.load()
    return {"version": __version__, "provider": settings.provider, "model": settings.model,
            "base_url": settings.base_url, "key_set": bool(settings.api_key and settings.api_key != "your-api-key"),
            "config_path": str(config_path()), "cwd": str(agent.cwd), "shell": agent.shell,
            "providers": {key: {"base_url": value[0], "model": value[1]} for key, value in PROVIDERS.items()}}


def save_settings(params: dict) -> None:
    path = config_path()
    original = read_env(path)
    provider = params.get("provider")
    if provider not in PROVIDERS:
        raise ConsoleError("Выберите провайдера")
    key = params.get("api_key", "")
    current = Settings.from_values(original)
    if not key and provider == current.provider:
        key = current.api_key
    values = read_env(path)
    values.update(AI_PROVIDER=provider, AI_API_BASE_URL=params.get("base_url", ""),
                  AI_MODEL=params.get("model", ""), AI_API_KEY=key,
                  AI_REQUIRE_KEY="false" if provider == "compatible" and not key else "true")
    settings = Settings.from_values(values)
    settings.validate_key()
    # JSON string quoting is not .env quoting: validate literal values first.
    if any(any(c in str(value) for c in "\r\n'") for value in values.values()):
        raise ConsoleError("Настройки содержат недопустимый символ")
    defaults = dict(line.split("=", 1) for line in template(provider).splitlines())
    defaults.update({key: value for key, value in values.items() if key in defaults})
    content = "\n".join(key + "='" + str(value) + "'" for key, value in defaults.items()) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(content)
    try:
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    agent = Agent()
    write_lock, busy_lock = Lock(), Lock()
    busy = False

    def send(identifier, *, result=None, error=None):
        with write_lock:
            print(json.dumps({"id": identifier, "result": result, "error": error}, ensure_ascii=False), flush=True)

    def dispatch(identifier, method, params):
        nonlocal busy
        result, error = None, None
        try:
            if method == "plan":
                result = agent.make_plan(params.get("prompt", ""), continuation=params.get("continuation", False), prepared=True)
            elif method == "execute":
                result = agent.execute(params.get("plan_id", ""), approval=params.get("approval", ""), prepared=True)
            else:
                raise ConsoleError("Неизвестный метод")
        except ConsoleError as exc:
            error = str(exc)
        except Exception:
            # Never serialize exceptions that might embed the API key or file contents.
            error = "Не удалось обработать запрос. Проверьте настройки и повторите."
        finally:
            with busy_lock:
                busy = False
        send(identifier, result=result, error=error)

    with ThreadPoolExecutor(max_workers=1) as pool:
        while True:
            line = sys.stdin.readline(65537)
            if not line:
                agent.cancel()
                break
            identifier = None
            try:
                if len(line) > 65536:
                    raise ConsoleError("Слишком большой запрос")
                request = json.loads(line)
                if not isinstance(request, dict) or not isinstance(request.get("params", {}), dict):
                    raise ConsoleError("Неверный формат запроса")
                identifier = request.get("id")
                method, params = request.get("method"), request.get("params", {})
                if method == "cancel":
                    agent.cancel()
                    send(identifier, result={"cancelled": True})
                elif method == "status":
                    send(identifier, result=status(agent))
                elif method in {"reset", "configure", "plan", "execute"}:
                    with busy_lock:
                        if busy:
                            raise ConsoleError("Дождитесь завершения текущего запроса")
                        if method in {"plan", "execute"}:
                            busy = True
                            agent.begin_request()
                            pool.submit(dispatch, identifier, method, params)
                            continue
                    if method == "reset":
                        agent.reset()
                    else:
                        save_settings(params)
                    send(identifier, result=status(agent))
                else:
                    raise ConsoleError("Неизвестный метод")
            except (ValueError, TypeError):
                send(identifier, error="Неверный формат JSON запроса")
            except ConsoleError as exc:
                send(identifier, error=str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
