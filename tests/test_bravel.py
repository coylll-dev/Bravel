from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from bravel.apps import find_steam_game, game_plan, SteamGame
from bravel.cli import main
from bravel.config import ConsoleError, Settings, read_env
from bravel.executor import approve, dangerous, execute, script_for, shell_argv
from bravel.planner import Planner, Plan, Step, local_fix, parse_plan
from bravel.ui import UI, safe_text


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ConfigTests(unittest.TestCase):
    def test_env_is_literal_and_does_not_expand_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("# comment\nAI_API_KEY='literal$(whoami)'\nexport AI_MODEL=small # note\n", encoding="utf-8")
            self.assertEqual(read_env(path), {"AI_API_KEY": "literal$(whoami)", "AI_MODEL": "small"})

    def test_environment_overrides_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AI_MODEL": "override"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text("AI_MODEL=file\n", encoding="utf-8")
            self.assertEqual(Settings.load(path).model, "override")

    def test_remote_http_and_invalid_ranges_are_rejected(self):
        for values in ({"AI_API_BASE_URL": "http://example.com/v1"}, {"AI_TIMEOUT": "0"}, {"AI_MAX_STEPS": "500"}, {"AI_TIMEOUT": "nan"}, {"AI_JSON_MODE": "maybe"}):
            with patch.dict(os.environ, values, clear=True), self.assertRaises(ConsoleError):
                Settings.load(Path("nonexistent-config"))

    def test_secret_not_in_settings_repr(self):
        self.assertNotIn("secret-key", repr(Settings(api_key="secret-key")))


class PlanningTests(unittest.TestCase):
    def test_local_fix_preserves_arguments(self):
        ctx = {"shell": "powershell", "available_commands": ["cmd", "git"]}
        self.assertEqual(local_fix("cdm /c echo hello", ctx).steps[0].command, "cmd /c echo hello")
        self.assertEqual(local_fix("gti status", ctx).steps[0].command, "git status")
        self.assertIsNone(local_fix("gti status; echo surprise", ctx))

    def test_unknown_or_unavailable_command_is_not_invented(self):
        ctx = {"shell": "bash", "available_commands": []}
        self.assertIsNone(local_fix("cdm", ctx))
        self.assertIsNone(local_fix("unrecognizable", ctx))

    def test_invalid_plans_never_execute(self):
        for command in ("", "echo ok\nrm x", "echo \x1b[2J", "echo \u202eabc"):
            content = json.dumps({"summary": "x", "steps": [{"command": command, "explanation": "x", "risk": "low"}]})
            with self.assertRaises(ConsoleError):
                parse_plan(content, 5)
        with self.assertRaises(ConsoleError):
            parse_plan('{"summary":"x","steps":[{},{}]}', 1)

    def test_markdown_json_works(self):
        plan = parse_plan('```json\n{"summary":"Need more info", "steps":[]}\n```', 5)
        self.assertEqual(plan.steps, ())


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan("Check location", (Step("pwd", "Show working directory"),))
        self.ui = UI("never", io.StringIO())

    def test_enter_eof_and_no_decline(self):
        for value in ("\n", "", "n\n", "anything\n"):
            with patch("sys.stdin", Terminal(value)):
                self.assertEqual(approve(self.plan, self.ui), ())

    def test_yes_accepts_exact_command(self):
        with patch("sys.stdin", Terminal("Y\n")):
            self.assertEqual(approve(self.plan, self.ui), self.plan.steps)

    def test_piped_yes_is_not_confirmation(self):
        with patch("sys.stdin", io.StringIO("y\n")):
            self.assertEqual(approve(self.plan, self.ui), ())

    def test_high_risk_requires_run_even_if_model_claims_low(self):
        step = Step("Remove-Item important.txt", "Delete", "low")
        self.assertTrue(dangerous(step))
        for answer, expected in (("y\n", ()), ("RUN\n", (step,))):
            with patch("sys.stdin", Terminal(answer)):
                self.assertEqual(approve(Plan("Delete", (step,)), self.ui), expected)

    def test_no_steps_never_starts_shell(self):
        with patch("bravel.executor.subprocess.run") as run:
            self.assertEqual(execute((), "powershell", self.ui), 0)
            run.assert_not_called()

    def test_execution_preserves_child_exit_code(self):
        with patch("bravel.executor.shutil.which", return_value="bash"), patch("bravel.executor.subprocess.run", return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(execute(self.plan.steps, "bash", self.ui), 7)
            self.assertFalse(run.call_args.kwargs["check"])
            self.assertEqual(run.call_args.args[0][1], "-c")

    def test_scripts_stop_on_error_and_preserve_comments(self):
        steps = (Step("false # comment", "Fail"), Step("echo unexpected", "Do not run"))
        self.assertIn("} &&\n{", script_for(steps, "bash"))
        self.assertIn("if (-not $?)", script_for(steps, "powershell"))

    @unittest.skipIf(os.name == "nt", "Native Linux Bash is checked by CI")
    def test_actual_bash_stops_after_failed_command(self):
        steps = (Step("false # comment", "Fail"), Step("echo SHOULD_NOT_RUN", "Do not run"))
        result = subprocess.run(shell_argv("bash", script_for(steps, "bash")), capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SHOULD_NOT_RUN", result.stdout)

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is not installed")
    def test_actual_powershell_stops_after_native_failure(self):
        failing = "cmd /d /c exit 7" if os.name == "nt" else "false"
        steps = (Step(failing, "Fail"), Step("Write-Output SHOULD_NOT_RUN", "Do not run"))
        result = subprocess.run(shell_argv("powershell", script_for(steps, "powershell")), capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b"SHOULD_NOT_RUN", result.stdout)

    def test_terminal_controls_are_not_displayed(self):
        self.assertEqual(safe_text("hello\x1b[2J\r\u202eworld"), "hello?[2J??world")


class APITests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.api_headers = []
        self.status = 200
        self.body = {"choices": [{"message": {"content": json.dumps({"summary": "Test", "steps": [{"command": "pwd", "explanation": "Directory", "risk": "low"}]})}}]}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, self.headers.get("Authorization"), payload))
                outer.api_headers.append(dict(self.headers))
                self.send_response(outer.status)
                if outer.status == 302:
                    self.send_header("Location", "http://localhost:1/stolen")
                self.end_headers()
                self.wfile.write(json.dumps(outer.body).encode())

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.settings = Settings(base_url=f"http://127.0.0.1:{self.server.server_port}/v1", api_key="test-key", timeout=2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_real_http_request_format_and_response_validation(self):
        plan = Planner(self.settings).make_plan("show directory", {"shell": "bash"})
        self.assertEqual(plan.steps[0].command, "pwd")
        path, authorization, payload = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(authorization, "Bearer test-key")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("test-key", json.dumps(payload))

    def test_errors_and_redirects_are_not_followed(self):
        for status in (401, 429, 302):
            self.status = status
            with self.assertRaises(ConsoleError) as caught:
                Planner(self.settings).make_plan("x", {})
            self.assertNotIn("test-key", str(caught.exception))
            self.assertIn(str(status), str(caught.exception))

    def test_malformed_response_rejected(self):
        self.body = {"unexpected": "shape"}
        with self.assertRaises(ConsoleError):
            Planner(self.settings).make_plan("x", {})

    def test_local_service_without_key(self):
        settings = Settings(base_url=self.settings.base_url, require_key=False, json_mode=False)
        Planner(settings).make_plan("x", {})
        _, authorization, payload = self.requests[0]
        self.assertIsNone(authorization)
        self.assertNotIn("response_format", payload)

    def test_native_gemini_uses_header_key_and_generate_content_format(self):
        content = self.body["choices"][0]["message"]["content"]
        self.body = {"candidates": [{"content": {"parts": [{"text": "hidden thinking", "thought": True}, {"text": content}]}}]}
        settings = Settings(provider="gemini", base_url=self.settings.base_url, api_key="gemini-test-key", model="models/gemini-3.8-flash")
        plan = Planner(settings).make_plan("show directory", {"shell": "bash"})
        self.assertEqual(plan.steps[0].command, "pwd")
        path, authorization, payload = self.requests[0]
        self.assertEqual(path, "/v1/models/gemini-3.8-flash:generateContent")
        self.assertIsNone(authorization)
        self.assertEqual(self.api_headers[0]["X-Goog-Api-Key"], "gemini-test-key")
        self.assertEqual(payload["generationConfig"], {"responseMimeType": "application/json"})
        self.assertIn("systemInstruction", payload)
        self.assertNotIn("gemini-test-key", path + json.dumps(payload))

    def test_gemini_blocked_response_cannot_produce_commands(self):
        self.body = {"promptFeedback": {"blockReason": "SAFETY"}}
        with self.assertRaises(ConsoleError):
            Planner(Settings(provider="gemini", base_url=self.settings.base_url, api_key="test", model="gemini-3.8-flash")).make_plan("x", {})

    def test_openrouter_uses_compatible_protocol_and_router_model_id(self):
        settings = Settings(provider="openrouter", base_url=self.settings.base_url, api_key="router-test-key", model="openrouter/auto")
        Planner(settings).make_plan("x", {})
        path, authorization, payload = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(authorization, "Bearer router-test-key")
        self.assertEqual(payload["model"], "openrouter/auto")


class GameTests(unittest.TestCase):
    def test_discovers_game_in_second_steam_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Steam"
            library = Path(directory) / "Games"
            (root / "steamapps").mkdir(parents=True)
            (library / "steamapps/common/Counter-Strike").mkdir(parents=True)
            (root / "steam.exe").touch()
            (root / "steam.sh").touch()
            vdf_path = str(library).replace("\\", "\\\\")
            (root / "steamapps/libraryfolders.vdf").write_text(f'"path" "{vdf_path}"', encoding="utf-8")
            (library / "steamapps/appmanifest_730.acf").write_text('"name" "Counter-Strike 2"\n"installdir" "Counter-Strike"', encoding="utf-8")
            with patch("bravel.apps.shutil.which", return_value=None):
                game = find_steam_game("730", [root])
            self.assertEqual(game.library, library)
            self.assertEqual(game.name, "Counter-Strike 2")

    def test_launch_path_is_quoted_and_requires_an_installed_game(self):
        game = SteamGame(Path("C:/Steam's folder/steam.exe"), Path("C:/Steam"), "730", "CS2")
        with patch("bravel.apps.find_steam_game", return_value=game):
            plan = game_plan("открой кс", "powershell")
            self.assertIn("Steam''s folder", plan.steps[0].command)
        with patch("bravel.apps.find_steam_game", return_value=None):
            self.assertEqual(game_plan("открой кс", "bash").steps, ())


class CLITests(unittest.TestCase):
    def test_powershell_command_file_is_empty_when_declined(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdin", Terminal("n\n")), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()), patch("bravel.cli.context", return_value={"shell": "powershell", "available_commands": ["cmd"]}):
            path = Path(directory) / "approved.ps1"
            self.assertEqual(main(["fix", "--shell", "powershell", "--command-file", str(path), "cdm"]), 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_powershell_command_file_contains_only_approved_script(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdin", Terminal("y\n")), patch("sys.stdout", io.StringIO()) as output, patch("sys.stderr", io.StringIO()), patch("bravel.cli.context", return_value={"shell": "powershell", "available_commands": ["cmd"]}):
            path = Path(directory) / "approved.ps1"
            self.assertEqual(main(["fix", "--shell", "powershell", "--command-file", str(path), "cdm"]), 0)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("cmd\n"))
            self.assertNotIn("BRAVEL", path.read_text(encoding="utf-8"))
            self.assertIn("BRAVEL", output.getvalue())

    def test_emit_mode_never_emits_when_declined(self):
        with patch("sys.stdin", Terminal("n\n")), patch("sys.stdout", io.StringIO()) as output, patch("sys.stderr", io.StringIO()), patch("bravel.cli.context", return_value={"shell": "powershell", "available_commands": ["cmd"]}):
            self.assertEqual(main(["fix", "--shell", "powershell", "--emit-command", "cdm"]), 0)
            self.assertEqual(output.getvalue(), "")

    def test_emit_mode_outputs_only_approved_script(self):
        with patch("sys.stdin", Terminal("y\n")), patch("sys.stdout", io.StringIO()) as output, patch("sys.stderr", io.StringIO()), patch("bravel.cli.context", return_value={"shell": "powershell", "available_commands": ["cmd"]}):
            self.assertEqual(main(["fix", "--shell", "powershell", "--emit-command", "cdm"]), 0)
            self.assertTrue(output.getvalue().startswith("cmd\n"))
            self.assertNotIn("BRAVEL", output.getvalue())

    def test_integration_paths_exist(self):
        for shell in ("powershell", "bash"):
            with patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(main(["integration", shell]), 0)
                self.assertTrue(Path(output.getvalue().strip()).is_file())


if __name__ == "__main__":
    unittest.main()
