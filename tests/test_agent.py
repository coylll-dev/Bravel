from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from bravel.agent import Agent
from bravel.bridge import save_settings, status
from bravel.config import ConsoleError, Settings
from bravel.planner import Plan, Step
from bravel.ui import UI
from bravel.apps import game_plan, SteamGame


class AgentTests(unittest.TestCase):
    def plan(self, agent, command="echo bravel-test", risk="low"):
        with patch("bravel.agent.game_plan", return_value=None), patch("bravel.agent.Planner.make_plan", return_value=Plan("test", (Step(command, "test", risk),))):
            return agent.make_plan("test")

    def test_preview_never_executes_and_tokens_are_single_use(self):
        agent = Agent(settings=Settings(require_key=False))
        with patch.object(agent, "_run", return_value={"exit_code": 0, "cancelled": False, "timed_out": False}) as run:
            plan = self.plan(agent)
            run.assert_not_called()
            with self.assertRaises(ConsoleError):
                agent.execute("forged", approval="approve")
            with self.assertRaises(ConsoleError):
                agent.execute(plan["plan_id"], approval="")
            agent.execute(plan["plan_id"], approval="approve")
            with self.assertRaises(ConsoleError):
                agent.execute(plan["plan_id"], approval="approve")
            run.assert_called_once()

    def test_risky_plan_requires_run_and_cancel_invalidates_plan(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = self.plan(agent, "echo test > overwritten.txt")
        self.assertTrue(plan["dangerous"])
        with self.assertRaises(ConsoleError):
            agent.execute(plan["plan_id"], approval="approve")
        agent.cancel()
        with self.assertRaises(ConsoleError):
            agent.execute(plan["plan_id"], approval="RUN")

    def test_new_request_invalidates_previous_plan(self):
        agent = Agent(settings=Settings(require_key=False))
        previous = self.plan(agent)
        self.plan(agent)
        with self.assertRaises(ConsoleError):
            agent.execute(previous["plan_id"], approval="approve")

    def test_cancel_before_worker_starts_never_plans_or_executes(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = self.plan(agent)
        agent.begin_request()
        agent.cancel()
        with patch("bravel.agent.Planner.make_plan") as planner, self.assertRaises(ConsoleError):
            agent.make_plan("test", prepared=True)
        planner.assert_not_called()
        with patch.object(agent, "_run") as run, self.assertRaises(ConsoleError):
            agent.execute(plan["plan_id"], approval="approve", prepared=True)
        run.assert_not_called()

    def test_command_output_reaches_next_plan_and_reset_clears_it(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = self.plan(agent)
        output = {"exit_code": 0, "cancelled": False, "timed_out": False, "output": "observed-output"}
        with patch.object(agent, "_run", return_value=output):
            agent.execute(plan["plan_id"], approval="approve")
        with patch("bravel.agent.Planner.make_plan", return_value=Plan("done", ())) as planner:
            agent.make_plan("", continuation=True)
            self.assertIn("observed-output", json.dumps(planner.call_args.args[1]))
        agent.reset()
        self.assertEqual(agent.history, [])
        with self.assertRaises(ConsoleError):
            agent.make_plan("", continuation=True)

    def test_actual_execution_captures_output_failure_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "destination"
            target.mkdir()
            agent = Agent(cwd=root, settings=Settings(require_key=False))
            command = "Set-Location 'destination'; Write-Output 'observed привет'" if os.name == "nt" else "cd destination; echo 'observed привет'"
            plan = self.plan(agent, command)
            result = agent.execute(plan["plan_id"], approval="approve")
            self.assertEqual(result["results"][0]["exit_code"], 0)
            self.assertIn("observed", result["results"][0]["output"])
            self.assertIn("привет", result["results"][0]["output"])
            self.assertEqual(agent.cwd, target.resolve())
            failure = "cmd /c exit 7" if os.name == "nt" else "false"
            plan = self.plan(agent, failure)
            with patch.object(agent, "_run", wraps=agent._run) as run:
                result = agent.execute(plan["plan_id"], approval="RUN" if plan["dangerous"] else "approve")
                self.assertNotEqual(result["results"][0]["exit_code"], 0)
                run.assert_called_once()

    def test_cancel_stops_running_process(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = self.plan(agent, "Start-Sleep -Seconds 30" if os.name == "nt" else "sleep 30")
        results = []
        thread = Thread(target=lambda: results.append(agent.execute(plan["plan_id"], approval="approve")))
        thread.start()
        import time
        time.sleep(0.3)
        agent.cancel()
        thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertTrue(results[0]["cancelled"])

    def test_all_panel_rows_have_closed_equal_width_borders(self):
        output = io.StringIO()
        with patch("bravel.ui.shutil.get_terminal_size", return_value=os.terminal_size((50, 24))):
            UI("never", output).panel("ПЛАН", "Длинный русский текст " * 9 + "\n\nкоманда")
        rows = output.getvalue().splitlines()
        self.assertTrue(rows[0].endswith("╮"))
        self.assertTrue(rows[-1].endswith("╯"))
        self.assertEqual(len({len(row) for row in rows}), 1)
        self.assertTrue(all(row.endswith("│") for row in rows[1:-1]))

    def test_deep_rock_launch_uses_installed_manifest(self):
        game = SteamGame(Path("Steam/steam.exe"), Path("library"), "548430", "Deep Rock Galactic")
        with patch("bravel.apps.find_steam_game", return_value=game) as finder:
            plan = game_plan("Запусти Deep Rock Galactic игру в стим", "cmd")
            finder.assert_called_once_with("548430")
            self.assertEqual(plan.steps[0].command, "start steam://rungameid/548430")

    def test_desktop_settings_never_return_secret_and_keep_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BRAVEL_ENV": str(Path(directory) / ".env")}, clear=True):
            save_settings({"provider": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "model": "example", "api_key": "test-secret"})
            self.assertNotIn("test-secret", json.dumps(status(Agent(cwd=Path(directory)))))
            save_settings({"provider": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta", "model": "changed", "api_key": ""})
            self.assertEqual(Settings.load().api_key, "test-secret")
            self.assertEqual(Settings.load().model, "changed")

