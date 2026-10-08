from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from typing import TextIO
from contextlib import contextmanager
from threading import Event, Thread
from .privacy import redact
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text


def safe_text(value: str) -> str:
    # No terminal escape sequences, bidi controls, or carriage-return rewriting.
    value = value.replace("\r\n", "\n")
    return redact("".join(c if c == "\n" or not unicodedata.category(c).startswith("C") else "?" for c in value))


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

    def clear(self) -> None:
        if self.stream.isatty():
            # This escape sequence is owned by the UI, never supplied by a model.
            if os.name == "nt":
                import subprocess
                subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "cls"], check=False)
            else:
                self.stream.write("\033[2J\033[H")
                self.stream.flush()

    @contextmanager
    def busy(self, message: str):
        if not self.stream.isatty():
            self.note(message)
            yield
            return
        stopped = Event()
        def animate():
            index = 0
            while not stopped.wait(0.15):
                self.stream.write("\r  " + "|/-\\"[index % 4] + " " + safe_text(message))
                self.stream.flush()
                index += 1
        self.note(message)
        thread = Thread(target=animate, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join()
            self.stream.write("\r" + " " * (len(message) + 6) + "\r")
            self.stream.flush()

    def panel(self, title: str, body: str, tone: str = "96", *, markdown: bool = False) -> None:
        width = max(12, min(shutil.get_terminal_size((88, 24)).columns - 3, 100))
        console = Console(file=self.stream, width=width + 2, height=24, force_terminal=self.color,
                          color_system="standard" if self.color else None, highlight=False,
                          legacy_windows=False, safe_box=False)
        content = Markdown(safe_text(body), hyperlinks=False) if markdown else Text(safe_text(body))
        style = {"96": "cyan", "93": "yellow", "91": "red"}.get(tone, "cyan")
        panel = Panel(content, title=Text(safe_text(title).replace("\n", " ")), title_align="left",
                      border_style=style, box=box.ROUNDED, padding=(0, 1), width=width)
        console.print(Padding(panel, (0, 0, 0, 2)))

    def answer(self, body: str, *, title: str = "ОТВЕТ") -> None:
        self.panel(title, body, markdown=True)

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
