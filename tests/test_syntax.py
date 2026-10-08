import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bravel.agent import Agent
from bravel.config import ConsoleError, Settings
from bravel.planner import Planner, PlanStructureError, PlanCheckError, parse_plan
from bravel.syntax import syntax_errors


def api_response(command):
    plan = {"summary": "test", "steps": [{"command": command, "explanation": "test", "risk": "low"}]}
    return plan_response(plan)


def plan_response(plan):
    payload = {"choices": [{"message": {"content": json.dumps(plan)}}]}
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return response


class SyntaxTests(unittest.TestCase):
    def test_disallowed_check_repaired_before_publication_and_never_executed(self):
        step = {"command": "Get-Process", "explanation": "test", "risk": "low"}
        for invalid_check in ("Get-Process | Where-Object Name -eq incy", "Remove-Item 'file.txt'", "pgrep -x -- 'incy'"):
            with self.subTest(check=invalid_check):
                opener = MagicMock()
                opener.open.side_effect = [plan_response({"summary": "test", "steps": [{**step, "check": invalid_check}]}), plan_response({"summary": "test", "steps": [step]})]
                agent = Agent(shell="powershell", settings=Settings(require_key=False))
                with patch("bravel.planner.build_opener", return_value=opener), patch("bravel.syntax.syntax_errors", return_value=[]), patch.object(agent, "_run") as execute:
                    published = agent.make_plan("Какой клиент работает?")
                    self.assertEqual(published["steps"][0]["check"], "")
                    with self.assertRaises(ConsoleError):
                        agent.execute(published["plan_id"], approval="")
                    execute.assert_not_called()
                self.assertEqual(opener.open.call_count, 2)
                request = json.loads(opener.open.call_args.args[0].data)
                repair = json.loads(request["messages"][1]["content"])["context"]["response_repair"]
                self.assertEqual(json.loads(repair["candidate"])["steps"][0]["check"], invalid_check)

    def test_repeated_invalid_check_stops_after_one_repair(self):
        plan = {"summary": "test", "steps": [{"command": "echo x", "explanation": "test", "risk": "low", "check": "Remove-Item 'file.txt'"}]}
        opener = MagicMock()
        opener.open.side_effect = [plan_response(plan), plan_response(plan)]
        with patch("bravel.planner.build_opener", return_value=opener), self.assertRaises(PlanCheckError):
            Planner(Settings(require_key=False)).make_plan("test", {"shell": "powershell", "agent_tools": {"processes": {}}})
        self.assertEqual(opener.open.call_count, 2)

    def test_check_allowlist_is_scoped_to_selected_shell(self):
        step = {"command": "echo x", "explanation": "test", "risk": "low", "check": "pgrep -x -- 'incy'"}
        content = json.dumps({"summary": "test", "steps": [step]})
        self.assertEqual(parse_plan(content, 5, shell="bash").steps[0].check, step["check"])
        with self.assertRaises(PlanCheckError):
            parse_plan(content, 5, shell="powershell")
        step["check"] = "[bool](Get-Process -Name 'incy' -ErrorAction SilentlyContinue)"
        self.assertEqual(parse_plan(json.dumps({"summary": "test", "steps": [step]}), 5, shell="powershell").steps[0].check, step["check"])

    def test_mixed_plan_repaired_to_one_stage_without_executing_candidates(self):
        step = {"command": "Get-NetRoute", "explanation": "routes", "risk": "low"}
        tool = {"name": "network_info", "arguments": {}}
        mixed = {"summary": "VPN", "steps": [step], "tools": [tool]}
        for corrected in ({"summary": "VPN", "steps": [], "tools": [tool]},
                          {"summary": "VPN", "steps": [step]}):
            with self.subTest(corrected=corrected):
                opener = MagicMock()
                opener.open.side_effect = [plan_response(mixed), plan_response(corrected)]
                with patch("bravel.planner.build_opener", return_value=opener), patch("bravel.syntax.syntax_errors", return_value=[]), patch("bravel.agent.run_tool") as tool_run, patch("bravel.agent.Agent._run") as shell_run:
                    plan = Planner(Settings(require_key=False)).make_plan("Включен VPN?", {"shell": "powershell", "agent_tools": {"network_info": {}}})
                self.assertEqual(bool(plan.steps), bool(corrected["steps"]))
                self.assertEqual(bool(plan.tools), bool(corrected.get("tools")))
                self.assertEqual(opener.open.call_count, 2)
                request = json.loads(opener.open.call_args.args[0].data)
                repair = json.loads(request["messages"][1]["content"])["context"]["response_repair"]
                self.assertEqual(json.loads(repair["candidate"]), mixed)
                self.assertIn("отдельными этапами", repair["error"])
                tool_run.assert_not_called()
                shell_run.assert_not_called()

    def test_structure_and_json_errors_share_one_repair_attempt(self):
        mixed = {"summary": "test", "steps": [{"command": "echo x", "explanation": "test", "risk": "low"}], "tools": [{"name": "network_info", "arguments": {}}]}
        for second in (mixed, "not JSON"):
            opener = MagicMock()
            if isinstance(second, dict):
                response = plan_response(second)
            else:
                response = MagicMock()
                response.__enter__.return_value = io.BytesIO(json.dumps({"choices": [{"message": {"content": second}}]}).encode())
            opener.open.side_effect = [plan_response(mixed), response]
            with patch("bravel.planner.build_opener", return_value=opener), self.assertRaises(ConsoleError):
                Planner(Settings(require_key=False)).make_plan("test", {"shell": "cmd", "agent_tools": {"network_info": {}}})
            self.assertEqual(opener.open.call_count, 2)

    def test_tool_count_limit_is_repaired_without_silently_truncating(self):
        tool = {"name": "network_info", "arguments": {}}
        opener = MagicMock()
        opener.open.side_effect = [plan_response({"summary": "test", "steps": [], "tools": [tool, tool]}), plan_response({"summary": "test", "steps": [], "tools": [tool]})]
        with patch("bravel.planner.build_opener", return_value=opener):
            plan = Planner(Settings(require_key=False, max_steps=1)).make_plan("test", {"shell": "cmd", "agent_tools": {"network_info": {}}})
        self.assertEqual(len(plan.tools), 1)
        self.assertEqual(opener.open.call_count, 2)

    def test_mixed_plan_without_agent_tools_does_not_retry(self):
        mixed = {"summary": "test", "steps": [{"command": "echo x", "explanation": "test", "risk": "low"}], "tools": [{"name": "network_info", "arguments": {}}]}
        opener = MagicMock()
        opener.open.side_effect = [plan_response(mixed)]
        with patch("bravel.planner.build_opener", return_value=opener), self.assertRaises(PlanStructureError):
            Planner(Settings(require_key=False)).make_plan("test", {"shell": "cmd"})
        self.assertEqual(opener.open.call_count, 1)

    def test_repaired_shell_plan_still_requires_approval(self):
        step = {"command": "echo routes", "explanation": "test", "risk": "low"}
        opener = MagicMock()
        opener.open.side_effect = [plan_response({"summary": "test", "steps": [step], "tools": [{"name": "network_info", "arguments": {}}]}), plan_response({"summary": "test", "steps": [step]})]
        agent = Agent(shell="cmd", settings=Settings(require_key=False))
        with patch("bravel.planner.build_opener", return_value=opener), patch("bravel.syntax.syntax_errors", return_value=[]), patch.object(agent, "_run") as execute:
            published = agent.make_plan("У меня сейчас включен VPN туннель?")
            with self.assertRaises(ConsoleError):
                agent.execute(published["plan_id"], approval="")
            execute.assert_not_called()
            execute.return_value = {"exit_code": 0, "output": "routes", "cancelled": False, "timed_out": False}
            agent.execute(published["plan_id"], approval="approve")
            execute.assert_called_once_with("echo routes")

    def test_unsafe_command_format_is_not_downgraded_to_structure_repair(self):
        opener = MagicMock()
        opener.open.side_effect = [api_response("echo x\necho y")]
        with patch("bravel.planner.build_opener", return_value=opener), self.assertRaises(ConsoleError):
            Planner(Settings(require_key=False)).make_plan("test", {"shell": "cmd", "agent_tools": {"network_info": {}}})
        self.assertEqual(opener.open.call_count, 1)

    def test_malformed_json_is_repaired_once_without_execution(self):
        malformed = MagicMock()
        malformed.__enter__.return_value = io.BytesIO(json.dumps({"choices": [{"message": {"content": "not JSON"}}]}).encode())
        opener = MagicMock()
        opener.open.side_effect = [malformed, api_response("echo repaired")]
        with patch("bravel.planner.build_opener", return_value=opener), patch("bravel.syntax.syntax_errors", return_value=[]):
            plan = Planner(Settings(require_key=False)).make_plan("task", {"shell": "cmd", "agent_tools": {"processes": {}}})
        self.assertEqual(plan.steps[0].command, "echo repaired")
        self.assertEqual(opener.open.call_count, 2)
        request = json.loads(opener.open.call_args.args[0].data)
        self.assertIn("response_repair", json.loads(request["messages"][1]["content"])["context"])

    def test_parse_only_never_creates_files_and_detects_invalid_source(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist.txt"
            if os.name == "nt":
                shell = "powershell"
                command = "Set-Content -LiteralPath '" + str(marker).replace("'", "''") + "' -Value test"
                malformed = "foreach ($p in @()) { $p } | Sort-Object"
            else:
                shell = "bash"
                import shlex
                command = "touch " + shlex.quote(str(marker))
                malformed = command + "; if"
            self.assertEqual(syntax_errors(shell, [command]), [])
            self.assertTrue(syntax_errors(shell, [malformed]))
            self.assertFalse(marker.exists())

    def test_parser_errors_reach_model_and_repaired_plan_is_returned(self):
        opener = MagicMock()
        opener.open.side_effect = [api_response("broken"), api_response("Get-Process")]
        errors = [{"index": 0, "message": "An empty pipe element is not allowed"}]
        with patch("bravel.planner.build_opener", return_value=opener), patch("bravel.syntax.syntax_errors", side_effect=[errors, []]):
            plan = Planner(Settings(require_key=False)).make_plan("processes", {"shell": "powershell", "agent_tools": {"processes": {}}})
        self.assertEqual(plan.steps[0].command, "Get-Process")
        self.assertEqual(opener.open.call_count, 2)
        request = json.loads(opener.open.call_args.args[0].data)
        user = json.loads(request["messages"][1]["content"])
        self.assertEqual(user["context"]["syntax_repair"]["errors"], errors)

    def test_repeated_parse_error_stops_without_unbounded_api_retries(self):
        opener = MagicMock()
        opener.open.side_effect = [api_response("broken"), api_response("still broken")]
        with patch("bravel.planner.build_opener", return_value=opener), patch("bravel.syntax.syntax_errors", return_value=[{"index": 0, "message": "invalid"}]):
            with self.assertRaises(ConsoleError):
                Planner(Settings(require_key=False)).make_plan("test", {"shell": "powershell", "agent_tools": {"processes": {}}})
        self.assertEqual(opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
