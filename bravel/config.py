from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConsoleError(Exception):
    """An error safe to display to a terminal user."""


PROVIDERS = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini", "OPENAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-3.8-flash", "GEMINI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "openrouter/auto", "OPENROUTER_API_KEY"),
    "compatible": ("http://localhost:11434/v1", "your-model", "AI_API_KEY"),
}


def template(provider: str = "openai") -> str:
    base_url, model, _ = PROVIDERS[provider]
    return f"AI_PROVIDER={provider}\nAI_API_BASE_URL={base_url}\nAI_API_KEY=your-api-key\nAI_MODEL={model}\nAI_TIMEOUT=45\nAI_JSON_MODE=true\nAI_MAX_STEPS=5\nAI_REQUIRE_KEY=true\nAI_COLOR=auto\n"


def config_path() -> Path:
    custom = os.environ.get("BRAVEL_ENV")
    return Path(custom).expanduser() if custom else Path.home() / ".config/bravel/.env"


def read_env(path: Path) -> dict[str, str]:
    """Read literal values: never source code, expand variables, or run substitutions."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise ConsoleError(f"Не удалось прочитать конфиг: {path}") from exc
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator or not key.strip().replace("_", "").isalnum():
            raise ConsoleError(f"Неверная строка {number} в конфиге {path}")
        value = value.strip()
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConsoleError(f"Незакрытая кавычка в строке {number}: {path}")
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def boolean(values: dict[str, str], key: str, default: bool) -> bool:
    value = values.get(key, str(default)).lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ConsoleError(f"{key}: ожидается true или false")
    return value in {"true", "1", "yes"}


@dataclass(frozen=True)
class Settings:
    provider: str = "openai"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = field(default="", repr=False)
    model: str = "gpt-4o-mini"
    timeout: float = 45
    json_mode: bool = True
    max_steps: int = 5
    require_key: bool = True
    color: str = "auto"

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        values = read_env(path or config_path())
        values.update(os.environ)
        return cls.from_values(values)

    @classmethod
    def from_values(cls, values: dict[str, str]) -> Settings:
        provider = values.get("AI_PROVIDER", "openai").lower()
        if provider not in PROVIDERS:
            raise ConsoleError("AI_PROVIDER: openai, gemini, openrouter или compatible")
        base_url, model, key_variable = PROVIDERS[provider]
        api_key = values.get("AI_API_KEY", "")
        if api_key in {"", "your-api-key"}:
            api_key = values.get(key_variable, api_key)
        try:
            settings = cls(
                provider=provider,
                base_url=values.get("AI_API_BASE_URL", base_url).rstrip("/"),
                api_key=api_key,
                model=values.get("AI_MODEL", model),
                timeout=float(values.get("AI_TIMEOUT", "45")),
                json_mode=boolean(values, "AI_JSON_MODE", True),
                max_steps=int(values.get("AI_MAX_STEPS", "5")),
                require_key=boolean(values, "AI_REQUIRE_KEY", True),
                color=values.get("AI_COLOR", "auto"),
            )
        except ValueError as exc:
            raise ConsoleError("AI_TIMEOUT и AI_MAX_STEPS должны быть числами") from exc
        parsed = urlsplit(settings.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConsoleError("AI_API_BASE_URL должен быть HTTP(S) URL без пароля, query и fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ConsoleError("Для удалённого API требуется HTTPS")
        if not 1 <= settings.timeout <= 300 or not 1 <= settings.max_steps <= 10:
            raise ConsoleError("AI_TIMEOUT: 1–300; AI_MAX_STEPS: 1–10")
        if settings.color not in {"auto", "always", "never"}:
            raise ConsoleError("AI_COLOR: auto, always или never")
        if not settings.model.strip():
            raise ConsoleError("AI_MODEL не может быть пустым")
        if settings.provider == "gemini" and not re.fullmatch(r"(?:models/)?[A-Za-z0-9_.-]+", settings.model):
            raise ConsoleError("AI_MODEL для Gemini должен быть идентификатором модели")
        if any(c in settings.api_key for c in "\r\n"):
            raise ConsoleError("API-ключ не может содержать переносы строк")
        return settings

    def validate_key(self) -> None:
        if self.require_key and (not self.api_key or self.api_key == "your-api-key"):
            raise ConsoleError(f"Укажите AI_API_KEY в {config_path()} или переменной окружения")
