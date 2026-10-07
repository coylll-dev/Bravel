from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bravel.agent import Agent
from bravel.apps import SteamGame
from bravel.chat import chat, run_task
from bravel.config import Settings, ConsoleError
from bravel.planner import Plan, Step
from bravel.ui import UI


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ChatTests(unittest.TestCase):
    def test_local_commands_do_not_call_api_and_new_clears_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "destination").mkdir()
            agent = Agent(cwd=root, settings=Settings(api_key="test-hidden-secret"))
            game = SteamGame(root / "steam.exe", root, "548430", "Deep Rock Galactic")
            output = io.StringIO()
            with patch("sys.stdin", Terminal()), patch("builtins.input", side_effect=["/help", "/games", "/history", "/cd destination", "/pwd", "/status", "/new", "/exit"]), patch("bravel.chat.installed_steam_games", return_value=[game]), patch("bravel.agent.Planner.make_plan") as planner:
                self.assertEqual(chat(agent, UI("never", output)), 0)
            planner.assert_not_called()
            self.assertIn("Deep Rock Galactic", output.getvalue())
            self.assertNotIn("test-hidden-secret", output.getvalue())
            self.assertEqual(agent.cwd, (root / "destination").resolve())
            self.assertEqual(agent.history, [])

    def test_task_executes_only_after_confirmation_and_analyzes_result(self):
        agent = Agent(settings=Settings(require_key=False))
        command = "Write-Output 'chat-result'" if os.name == "nt" else "echo chat-result"
        plans = [Plan("action", (Step(command, "fixture"),)), Plan("done", ())]
        with patch("sys.stdin", Terminal("y\n")), patch("bravel.agent.game_plan", return_value=None), patch("bravel.agent.Planner.make_plan", side_effect=plans) as planner:
            output = io.StringIO()
            self.assertEqual(run_task(agent, UI("never", output), "test"), 0)
            self.assertIn("chat-result", output.getvalue())
            self.assertEqual(planner.call_count, 2)
            self.assertIn("chat-result", str(planner.call_args.args[1]["history"]))

    def test_declined_plan_never_runs_or_continues(self):
        agent = Agent(settings=Settings(require_key=False))
        with patch("sys.stdin", Terminal("n\n")), patch("bravel.agent.game_plan", return_value=None), patch("bravel.agent.Planner.make_plan", return_value=Plan("action", (Step("echo no", "fixture"),))) as planner, patch.object(agent, "_run") as run:
            self.assertEqual(run_task(agent, UI("never", io.StringIO()), "test"), 0)
            run.assert_not_called()
            planner.assert_called_once()
            self.assertIsNone(agent.pending)

    def test_chat_requires_terminal_and_recoverable_errors_keep_session(self):
        agent = Agent(settings=Settings(require_key=False))
        with patch("sys.stdin", io.StringIO()), self.assertRaises(ConsoleError):
            chat(agent, UI("never", io.StringIO()))
        output = io.StringIO()
        with patch("sys.stdin", Terminal()), patch("builtins.input", side_effect=["/unknown", "/continue", "/exit"]):
            self.assertEqual(chat(agent, UI("never", output)), 0)
        self.assertIn("Неизвестная команда", output.getvalue())
        self.assertIn("Сначала выполните", output.getvalue())

    def test_keyboard_interrupt_kills_running_command(self):
        agent = Agent(settings=Settings(require_key=False))
        command = "Start-Sleep -Seconds 30" if os.name == "nt" else "sleep 30"
        with patch.object(agent._cancel, "wait", side_effect=KeyboardInterrupt), patch.object(agent, "_stop_process", wraps=agent._stop_process) as stop:
            with self.assertRaises(KeyboardInterrupt):
                agent._run(command)
            stop.assert_called_once()
            self.assertIsNotNone(stop.call_args.args[0].poll())
