from __future__ import annotations

import os
import shutil
import sys
import textwrap
import unicodedata
from typing import TextIO


def safe_text(value: str) -> str:
    # No terminal escape sequences, bidi controls, or carriage-return rewriting.
    return "".join(c if c == "\n" or not unicodedata.category(c).startswith("C") else "?" for c in value)


class UI:
    def __init__(self, color: str = "auto", stream: TextIO | None = None, *, terminal: bool = False):
        self.stream = stream or sys.stderr
        self.color = color == "always" or (color == "auto" and (self.stream.isatty() or terminal) and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")

    def paint(self, text: str, code: str) -> str:
        text = safe_text(text)
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def write(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def banner(self) -> None:
        self.write(self.paint("  ◆  BRAVEL", "1;96") + self.paint("  /  твоя консоль, с помощником", "90"))

    def note(self, message: str) -> None:
        self.write(self.paint("  · " + message, "90"))

    def error(self, message: str) -> None:
        self.write(self.paint("  ✕ " + message, "91"))

    def panel(self, title: str, body: str, tone: str = "96") -> None:
        width = max(12, min(shutil.get_terminal_size((88, 24)).columns - 3, 100))
        title = safe_text(title).replace("\n", " ")[:width - 6]
        self.write(self.paint("  ╭─ " + title + " " + "─" * (width - len(title) - 5) + "╮", tone))
        for line in safe_text(body).splitlines():
            for part in textwrap.wrap(line, width - 4, replace_whitespace=False, drop_whitespace=False) or [""]:
                self.write(self.paint("  │ ", tone) + part.ljust(width - 4) + self.paint(" │", tone))
        self.write(self.paint("  ╰" + "─" * (width - 2) + "╯", tone))

    def confirm(self, *, dangerous: bool = False) -> bool:
        if not sys.stdin.isatty():
            self.note("Для подтверждения нужен интерактивный терминал. Команда не выполнена.")
            return False
        prompt = "  Выполнить? Введите RUN для подтверждения: " if dangerous else "  Выполнить? [Y/n] (Enter = n): "
        self.stream.write(self.paint(prompt, "93" if dangerous else "1;96"))
        self.stream.flush()
        try:
            answer = sys.stdin.readline().strip()
        except (EOFError, KeyboardInterrupt):
            self.write()
            return False
        return answer == "RUN" if dangerous else answer.lower() in {"y", "yes", "д", "да"}
