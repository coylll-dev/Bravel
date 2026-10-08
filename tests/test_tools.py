import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bravel.agent import Agent
from bravel.apps import SteamGame
from bravel.chat import run_task
from bravel.cli import parser
from bravel.config import ConsoleError, Settings
from bravel.planner import Plan, parse_plan
from bravel.tools import ToolCall, run, validate, group_processes
from bravel.ui import UI


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ToolTests(unittest.TestCase):
    def test_model_selects_games_tool_and_answers_from_real_result(self):
        agent = Agent(settings=Settings(require_key=False))
        game = SteamGame(Path("steam.exe"), Path("library"), "548430", "Deep Rock Galactic")
        plans = [Plan("Посмотрю установленные игры", (), (ToolCall("games", {}),)), Plan("Найдена Deep Rock Galactic", ())]
        output = io.StringIO()
        with patch("bravel.agent.Planner.make_plan", side_effect=plans) as model, patch("bravel.apps.installed_steam_games", return_value=[game]), patch.object(UI, "confirm") as confirm:
            self.assertEqual(run_task(agent, UI("never", output), "Что у меня из игр?"), 0)
        confirm.assert_not_called()
        self.assertIn("Deep Rock Galactic", output.getvalue())
        context = model.call_args.args[1]
        self.assertIn("agent_tools", context)
        self.assertIn("548430", str(context["history"]))
        self.assertIsNone(agent.pending)

    def test_invalid_tools_and_mixed_plans_rejected(self):
        for tool in ({"name": "run_shell", "arguments": {"command": "del x"}}, {"name": "games", "arguments": {"unexpected": "x"}}, {"name": "read_text", "arguments": {"path": 1}}):
            with self.assertRaises(ConsoleError):
                parse_plan(json.dumps({"summary": "test", "steps": [], "tools": [tool]}), 5)
        with self.assertRaises(ConsoleError):
            parse_plan(json.dumps({"summary": "test", "steps": [{"command": "echo x", "risk": "low", "explanation": "test"}], "tools": [{"name": "games", "arguments": {}}]}), 5)

    def test_file_read_requires_confirmation_and_decline_never_reads(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = Plan("read", (), (ToolCall("read_text", {"path": "file.txt"}),))
        with patch("sys.stdin", Terminal("n\n")), patch("bravel.agent.Planner.make_plan", return_value=plan), patch("bravel.agent.run_tool") as tool:
            run_task(agent, UI("never", io.StringIO()), "read file")
        tool.assert_not_called()
        self.assertIsNone(agent.pending)

    def test_preview_never_runs_metadata_tools(self):
        agent = Agent(settings=Settings(require_key=False))
        agent.mode = "preview"
        with patch("bravel.agent.Planner.make_plan", return_value=Plan("read", (), (ToolCall("apps", {}),))), patch("bravel.agent.run_tool") as tool:
            run_task(agent, UI("never", io.StringIO()), "apps")
        tool.assert_not_called()

    def test_text_write_requires_run_backups_and_verifies_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "note.txt"
            file.write_text("old text", encoding="utf-8")
            agent = Agent(cwd=root, settings=Settings(require_key=False))
            plan = Plan("write", (), (ToolCall("write_text", {"path": "note.txt", "content": "new привет"}),))
            agent.pending = ("capability", plan)
            with self.assertRaises(ConsoleError):
                agent.execute("capability", approval="approve")
            self.assertEqual(file.read_text(), "old text")
            result = agent.execute("capability", approval="RUN")
            data = result["results"][0]["data"]
            self.assertTrue(data["verified"])
            self.assertEqual(file.read_text(encoding="utf-8"), "new привет")
            self.assertEqual(Path(data["backup"]).read_text(), "old text")
            with self.assertRaises(ConsoleError):
                agent.execute("capability", approval="RUN")

    def test_credential_files_and_secret_content_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in (".env", "id_rsa", "server.pem", "credentials"):
                with self.assertRaises(ConsoleError):
                    run(ToolCall("read_text", {"path": name}), Path(directory))
            with self.assertRaises(ConsoleError):
                validate(ToolCall("write_text", {"path": "note.txt", "content": "API_KEY=secret-value"}))

    def test_file_search_and_read_are_bounded_and_skip_dependency_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.txt").write_text("A" * 33000)
            (root / ".venv").mkdir()
            (root / ".venv/skip.txt").write_text("skip")
            data = run(ToolCall("find_files", {"pattern": "*.txt"}), root)
            self.assertEqual([Path(value).resolve() for value in data["matches"]], [(root / "a.txt").resolve()])
            data = run(ToolCall("read_text", {"path": "a.txt"}), root)
            self.assertTrue(data["truncated"])
            self.assertEqual(len(data["text"]), 32000)

    def test_failed_tool_does_not_loop_with_same_arguments(self):
        agent = Agent(settings=Settings(require_key=False))
        plan = Plan("read", (), (ToolCall("read_text", {"path": "absent-file.txt"}),))
        with patch("bravel.agent.Planner.make_plan", return_value=plan):
            result = agent.make_plan("test")
            agent.execute(result["plan_id"], approval="approve")
            with self.assertRaises(ConsoleError):
                agent.make_plan("", continuation=True)

    def test_special_inventory_subcommands_removed(self):
        sub = next(action for action in parser()._actions if hasattr(action, "choices") and isinstance(action.choices, dict))
        self.assertNotIn("games", sub.choices)
        self.assertNotIn("apps", sub.choices)

    def test_search_finds_directory_case_insensitively_and_reports_depth_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "INCY Client"
            target.mkdir()
            deep = root
            for index in range(7):
                deep = deep / str(index)
                deep.mkdir()
            data = run(ToolCall("find_files", {"pattern": "*incy*"}), root)
            self.assertIn(str(target), data["matches"])
            self.assertEqual(data["root"], str(root))
            self.assertTrue(data["limited"])
            self.assertIn("max_depth", data["incomplete_reasons"])

    def test_app_query_includes_installation_records_without_inventing_executable(self):
        record = {"name": "INCY", "install_location": "C:/Apps/Incy", "source": "Windows Uninstall registry"}
        with patch("bravel.programs.registered_software", return_value=[record]), patch("bravel.programs.installed_applications", return_value=[]):
            data = run(ToolCall("apps", {"query": "iNcY"}), Path.cwd())
        self.assertEqual(data["apps"], [record])
        self.assertFalse(data["coverage_complete"])
        self.assertNotIn("executable", data["apps"][0])

    def test_process_groups_keep_unknown_app_and_filter_by_path(self):
        entries = [{"name": "chrome", "pid": index, "executable": "/opt/chrome"} for index in range(250)]
        entries += [{"name": "incy", "pid": 900, "executable": "/opt/INCY/client"}]
        data = group_processes(entries)
        self.assertEqual(data["total_groups"], 2)
        self.assertEqual(data["processes"][0]["count"], 250)
        self.assertLessEqual(len(data["processes"][0]["pids"]), 20)
        self.assertEqual(group_processes(entries, "INCY/client")["processes"][0]["name"], "incy")

    def test_large_inventory_is_summarized_without_json_or_duplicate_list(self):
        agent = Agent(settings=Settings(require_key=False))
        plans = [Plan("Проверю процессы", (), (ToolCall("processes", {}),)), Plan("Найден INCY", ())]
        data = group_processes([{"name": "incy", "pid": 123, "executable": "/opt/INCY/client"}])
        output = io.StringIO()
        with patch("bravel.agent.Planner.make_plan", side_effect=plans), patch("bravel.agent.run_tool", return_value=data):
            run_task(agent, UI("never", output), "Какой клиент запущен?")
        self.assertIn("Найден INCY", output.getvalue())
        self.assertNotIn('"pids"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
