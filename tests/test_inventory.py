import io
import unittest
from pathlib import Path
from unittest.mock import patch

from bravel.agent import Agent
from bravel.apps import SteamGame
from bravel.chat import run_task
from bravel.cli import main
from bravel.config import ConsoleError, Settings
from bravel.inventory import inventory_plan
from bravel.planner import Plan, Step
from bravel.ui import UI


class Terminal(io.StringIO):
    def isatty(self):
        return True


class InventoryTests(unittest.TestCase):
    def test_games_question_uses_local_steam_inventory_without_api(self):
        game = SteamGame(Path("steam.exe"), Path("library"), "548430", "Deep Rock Galactic")
        agent = Agent(settings=Settings(require_key=False))
        with patch("bravel.inventory.installed_steam_games", return_value=[game]), patch("bravel.agent.Planner.make_plan") as api:
            plan = agent.make_plan("Какие у меня игры установлены?")
        api.assert_not_called()
        self.assertEqual(plan["steps"], [])
        self.assertIn(game.name, plan["summary"])
        self.assertIsNone(agent.pending)

    def test_command_request_gives_builtin_and_does_not_scan(self):
        with patch("bravel.inventory.installed_applications") as discovery:
            plan = inventory_plan("команда для вывода приложений установленных")
        discovery.assert_not_called()
        self.assertIn("bravel apps", plan.summary)
        self.assertEqual(plan.steps, ())
        self.assertIsNone(inventory_plan("Удали установленные игры"))
        self.assertIsNone(inventory_plan("Запусти установленную игру"))

    def test_short_cli_query_is_ask_and_inventory_cli_requires_no_config(self):
        for arguments in (["команда", "для", "вывода", "приложений"], ["ask", "--shell", "cmd", "команда для вывода приложений"]):
            with patch("sys.stdout", io.StringIO()), patch("bravel.cli.Planner.make_plan") as api:
                self.assertEqual(main(arguments), 0)
            api.assert_not_called()
        with patch("sys.stdout", io.StringIO()), patch("bravel.cli.listing", return_value="fixture"), patch("bravel.cli.Settings.load") as settings:
            self.assertEqual(main(["games"]), 0)
        settings.assert_not_called()

    def test_interactive_failure_stops_automatic_analysis(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = Plan("try", (Step("winget list", "fixture"),))
        failure = {"exit_code": 1, "output": "0x8a150042 : Error reading input in prompt", "cancelled": False, "timed_out": False, "truncated": False}
        with patch("sys.stdin", Terminal("y\n")), patch("bravel.agent.Planner.make_plan", return_value=plan) as api, patch.object(agent, "_run", return_value=failure):
            self.assertEqual(run_task(agent, UI("never", io.StringIO()), "test"), 1)
        api.assert_called_once()
        self.assertTrue(agent.last_result["results"][0]["needs_interaction"])

    def test_unchanged_failed_command_blocked_only_in_continuation(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = Plan("try", (Step("echo fixture", "fixture"),))
        with patch("bravel.agent.Planner.make_plan", return_value=plan), patch.object(agent, "_run", return_value={"exit_code": 1, "cancelled": False, "timed_out": False, "output": "failed"}):
            first = agent.make_plan("test")
            agent.execute(first["plan_id"], approval="approve")
            with self.assertRaises(ConsoleError):
                agent.make_plan("", continuation=True)
            self.assertIsNone(agent.pending)
            self.assertEqual(len(agent.make_plan("try again explicitly")["steps"]), 1)


if __name__ == "__main__":
    unittest.main()
