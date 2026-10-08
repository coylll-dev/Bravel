import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bravel.config import ConsoleError, Settings
from bravel.planner import Planner
from bravel.syntax import syntax_errors


def api_response(command):
    plan = {"summary": "test", "steps": [{"command": command, "explanation": "test", "risk": "low"}]}
    payload = {"choices": [{"message": {"content": json.dumps(plan)}}]}
    response = MagicMock()
    response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return response


class SyntaxTests(unittest.TestCase):
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
