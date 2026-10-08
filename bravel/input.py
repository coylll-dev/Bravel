"""Editable prompts with in-memory history; no implicit history file."""
from __future__ import annotations

import sys

COMMANDS = ("/help", "/clear", "/exit", "/quit", "/pwd", "/cd", "/shell", "/status",
            "/games", "/apps", "/continue", "/history", "/new", "/mode", "/save", "/load",
            "/sessions", "/delete", "/context", "/privacy")


def reader(ui, *, plain: bool = False):
    try:
        if plain or not sys.stdin.isatty() or not sys.stdout.isatty():
            raise OSError()
        sys.stdin.fileno()
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import NestedCompleter, WordCompleter, Completer
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.key_binding import KeyBindings
        bindings = KeyBindings()
        @bindings.add("escape", "enter")
        def newline(event):
            event.current_buffer.insert_text("\n")
        @bindings.add("tab")
        def complete(event):
            if event.current_buffer.complete_state:
                event.current_buffer.complete_next()
            else:
                event.current_buffer.start_completion(select_first=True)
        parameters = NestedCompleter.from_nested_dict({
            "/shell": {"cmd": None, "powershell": None, "bash": None},
            "/mode": {"ask": None, "preview": None, "read-only": None},
        })
        words = WordCompleter(list(COMMANDS), WORD=True)
        class ChatCompleter(Completer):
            def get_completions(self, document, event):
                completer = parameters if " " in document.text_before_cursor.lstrip() else words
                yield from completer.get_completions(document, event)
        session = PromptSession(completer=ChatCompleter(), auto_suggest=AutoSuggestFromHistory(), key_bindings=bindings,
            complete_while_typing=False, enable_history_search=False)
        return lambda: session.prompt("\n  bravel › ")
    except (ImportError, OSError):
        return lambda: input(ui.paint("\n  bravel › ", "1;96"))
