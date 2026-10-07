from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from bravel.agent import Agent
from bravel.config import ConsoleError, Settings
from bravel.planner import Plan, Step
from bravel.ui import UI, safe_text
from bravel.apps import game_plan, SteamGame


class AgentTests(unittest.TestCase):
    def test_crlf_output_does_not_gain_question_marks(self):
        self.assertEqual(safe_text("192.0.2.1\r\nsecond\r\n"), "192.0.2.1\nsecond\n")
        self.assertEqual(safe_text("rewrite\rhidden\x1b"), "rewrite?hidden?")

    def test_launched_child_holding_output_does_not_crash_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "launch.py"
            pid_file = root / "child.pid"
            script.write_text("import subprocess, sys\nfrom pathlib import Path\n"
                              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                              "Path('child.pid').write_text(str(child.pid))\n"
                              "print('launched', flush=True)\n", encoding="utf-8")
            agent = Agent(cwd=root, settings=Settings(require_key=False))
            command = (f"& '{sys.executable.replace(chr(39), chr(39)*2)}' '{script}'" if os.name == "nt"
                       else f"'{sys.executable}' '{script}'")
            try:
                result = agent._run(command)
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("launched", result["output"])
                self.assertNotIn("?", result["output"])
                result = agent._run("Write-Output 'still alive'" if os.name == "nt" else "echo 'still alive'")
                self.assertIn("still alive", result["output"])
            finally:
                if pid_file.exists():
                    pid = int(pid_file.read_text())
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        import signal
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                for temporary in agent._command_directories:
                    temporary.cleanup()
                self.assertTrue(all(not Path(temporary.name).exists() for temporary in agent._command_directories))

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

