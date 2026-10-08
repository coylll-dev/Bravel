from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import install
from bravel.agent import Agent
from bravel.chat import chat, run_task
from bravel.cli import main
from bravel.config import ConsoleError, Settings
from bravel.executor import approve, risk_reasons
from bravel.planner import Plan, Step, parse_plan, Planner
from bravel.policy import read_only
from bravel.privacy import redact, redact_data
from bravel.programs import Application, launch_plan, installed_applications
from bravel.sessions import Sessions
from bravel.ui import UI


class Terminal(io.StringIO):
    def isatty(self):
        return True


class FeaturesTests(unittest.TestCase):
    def test_empty_cli_and_agent_start_chat_without_api(self):
        for arguments in ([], ["agent"], ["agent", "--shell", "cmd"]):
            with patch("bravel.chat.chat", return_value=0) as interactive, patch("bravel.chat.run_task") as task:
                self.assertEqual(main(arguments), 0)
            interactive.assert_called_once()
            task.assert_not_called()

    def test_version_flag_typo_has_suggestion_without_api(self):
        output = io.StringIO()
        with patch("sys.stderr", output), patch("bravel.agent.Planner.make_plan") as model:
            with self.assertRaises(SystemExit) as error:
                main(["--versino"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("--version", output.getvalue())
        self.assertIn("Возможно", output.getvalue())
        model.assert_not_called()

    def test_editor_completion_history_and_multiline_input(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from bravel.input import reader
        stdin = MagicMock()
        stdin.isatty.return_value = True
        stdin.fileno.return_value = 0
        with create_pipe_input() as pipe:
            def session_factory(**kwargs):
                return PromptSession(input=pipe, output=DummyOutput(), **kwargs)
            with patch("sys.stdin", stdin), patch("sys.stdout.isatty", return_value=True), patch("prompt_toolkit.PromptSession", side_effect=session_factory):
                read = reader(UI("never", io.StringIO()))
                from threading import Timer
                pipe.send_text("/hel\t")
                enter = Timer(0.3, lambda: pipe.send_text("\r"))
                enter.start()
                try:
                    self.assertEqual(read(), "/help")
                finally:
                    enter.join()
                for text, expected in (("first\r", "first"), ("\x1b[A\r", "first"), ("a\nb\nc\r", "a\nb\nc"), ("a\x1b\rb\r", "a\nb")):
                    typed = Timer(0.2, lambda value=text: pipe.send_text(value))
                    typed.start()
                    try:
                        self.assertEqual(read(), expected)
                    finally:
                        typed.join()

    def test_display_formatting_does_not_trigger_disk_risk_but_mutations_do(self):
        from bravel.executor import dangerous
        command = "Get-Process | Sort-Object WorkingSet -Descending | Select-Object -First 10 Name, @{Name='Memory (MB)';Expression={'{0:N2}' -f ($_.WorkingSet / 1MB)}} | Format-Table -AutoSize"
        self.assertFalse(dangerous(Step(command, "memory", "low")))
        self.assertEqual(risk_reasons(Step(command, "memory", "low")), [])
        for formatter in ("Format-Table", "Format-List", "Format-Wide", "Format-Custom", "Format-Hex"):
            self.assertFalse(dangerous(Step("Get-Process | " + formatter, "display", "low")))
        for command in ("format C:", "format.exe D:", "Format-Volume -DriveLetter D", "Format-Disk", "Get-Process | Format-Table; Remove-Item file", "Get-Process | Format-Table > file"):
            self.assertTrue(dangerous(Step(command, "mutation", "low")), command)

    def test_help_and_completion_expose_chat_controls_not_inventory_commands(self):
        from bravel.chat import HELP
        from bravel.input import COMMANDS
        for command in ("/apps", "/games"):
            self.assertNotIn(command, HELP)
            self.assertNotIn(command, COMMANDS)
        self.assertIn("Ctrl+J", HELP)
        self.assertNotIn("Alt+Enter", HELP)

    def test_builtin_exit_and_clear_do_not_call_api(self):
        for exit_command in ("exit", "quit", "/exit", "/quit"):
            agent = Agent(settings=Settings(require_key=False))
            ui = UI("never", io.StringIO())
            with patch("sys.stdin", Terminal()), patch("builtins.input", side_effect=["clear", "cls", "/clear", exit_command]), patch.object(ui, "clear") as clear, patch("bravel.agent.Planner.make_plan") as model:
                self.assertEqual(chat(agent, ui, plain=True), 0)
            self.assertEqual(clear.call_count, 3)
            model.assert_not_called()

    def test_version_alias_matches_flag(self):
        for arguments in (["version"], ["--version"]):
            output = io.StringIO()
            with patch("sys.stdout", output):
                try:
                    self.assertEqual(main(arguments), 0)
                except SystemExit as exc:
                    self.assertEqual(exc.code, 0)
            from bravel import __version__
            self.assertEqual(output.getvalue().strip(), __version__)

    def test_preview_never_executes_or_requests_approval(self):
        agent = Agent(settings=Settings(require_key=False))
        agent.mode = "preview"
        with patch("bravel.agent.Planner.make_plan", return_value=Plan("read", (Step("pwd", "read"),))), patch.object(agent, "_run") as run, patch.object(UI, "confirm") as confirm:
            run_task(agent, UI("never", io.StringIO()), "test")
        run.assert_not_called()
        confirm.assert_not_called()

    def test_read_only_allowlist_rejects_injection_and_mutations(self):
        for shell, command in (("bash", "pwd"), ("powershell", "Get-Location"), ("cmd", "whoami")):
            self.assertTrue(read_only(command, shell))
            for injection in (command + "; rm -rf /", command + " && del x", command + " > file", command + " $(touch x)"):
                self.assertFalse(read_only(injection, shell))
        self.assertFalse(read_only("Get-Content ~/.config/bravel/.env", "powershell"))
        self.assertFalse(read_only("cat ~/.ssh/id_rsa", "bash"))
        self.assertFalse(read_only("echo hi", "cmd"))

    def test_read_only_mode_auto_allows_only_whole_safe_plan(self):
        ui = UI("never", io.StringIO())
        safe = Plan("read", (Step("pwd", "read"),))
        with patch.object(ui, "confirm", return_value=False) as confirm:
            self.assertEqual(approve(safe, ui, mode="read-only", shell="bash"), safe.steps)
            confirm.assert_not_called()
            self.assertEqual(approve(Plan("mixed", safe.steps + (Step("touch file", "write"),)), ui, mode="read-only", shell="bash"), ())
            confirm.assert_called_once()

    def test_local_risk_reason_overrides_low_model_rating(self):
        self.assertIn("удаление или изменение диска", risk_reasons(Step("Remove-Item file", "claimed safe", "low")))

    def test_check_schema_rejects_mutating_checks(self):
        for check in ("touch file", "pwd; rm file", "Get-Location | Invoke-Expression"):
            with self.assertRaises(ConsoleError):
                parse_plan(json.dumps({"summary": "test", "steps": [{"command": "echo test", "explanation": "test", "risk": "low", "check": check}]}), 5)

    def test_actual_file_check_observes_success_and_missing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(cwd=Path(directory), settings=Settings(require_key=False))
            if os.name == "nt":
                command, check = "New-Item -ItemType File -Path 'made.txt'", "Test-Path -LiteralPath 'made.txt'"
                missing = "Test-Path -LiteralPath 'absent.txt'"
            else:
                command, check = "touch made.txt", "test -e 'made.txt'"
                missing = "test -e 'absent.txt'"
            for identifier, step, expected in (("create", Step(command, "create", "high", check), "observed"),
                                                ("missing", Step("echo checked", "no change", "low", missing), "not_observed")):
                agent.pending = (identifier, Plan("test", (step,)))
                result = agent.execute(identifier, approval="RUN" if identifier == "create" else "approve")
                self.assertEqual(result["results"][0]["verification"]["status"], expected)

    def test_known_and_typical_secrets_redacted_in_nested_payload(self):
        token = "sk-proj-" + "A" * 30
        text = f'AI_API_KEY=custom-secret\n{{"password": "hunter2"}}\nBearer {token}\nhttps://me:password@example.com\n-----BEGIN PRIVATE KEY-----\nraw-key\n-----END PRIVATE KEY-----'
        output = json.dumps(redact_data({"history": [text, {"output": "configured-secret"}]}, ("configured-secret",)))
        for secret in ("custom-secret", "hunter2", token, "raw-key", "configured-secret"):
            self.assertNotIn(secret, output)
        self.assertEqual(redact("ordinary output 123"), "ordinary output 123")

    def test_model_request_masks_key_but_keeps_authentication_header(self):
        settings = Settings(api_key="unique-configured-secret")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {"content": '{"summary":"done","steps":[]}'}}]}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch("bravel.planner.build_opener", return_value=opener):
            Planner(settings).make_plan("unique-configured-secret", {"history": [{"output": "unique-configured-secret"}]})
        request = opener.open.call_args.args[0]
        self.assertNotIn(b"unique-configured-secret", request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer unique-configured-secret")

    def test_saved_session_is_redacted_and_load_never_restores_pending_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Sessions(Path(directory))
            agent = Agent(settings=Settings(api_key="unique-secret"))
            agent.history = [{"role": "user", "text": "unique-secret"}, {"role": "command_results", "results": [{"exit_code": 0, "output": "token=secret-value"}]}]
            agent.pending = ("execute-me", Plan("unsafe", (Step("del file", "test"),)))
            store.save("work", agent)
            raw = store.path("work").read_text(encoding="utf-8")
            self.assertNotIn("unique-secret", raw)
            self.assertNotIn("secret-value", raw)
            self.assertNotIn("execute-me", raw)
            store.load("work", agent)
            self.assertIsNone(agent.pending)
            self.assertIsNone(agent.last_result)
            self.assertTrue(all(item["historical"] for item in agent.history))
            with self.assertRaises(ConsoleError):
                agent.execute("execute-me", approval="RUN")
            self.assertEqual(store.names(), ["work"])
            store.delete("work")
            self.assertEqual(store.names(), [])

    def test_session_names_and_malformed_history_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Sessions(Path(directory))
            for name in ("../key", "a/b", "", "a" * 65):
                with self.assertRaises(ConsoleError):
                    store.path(name)
            store.path("bad").write_text(json.dumps({"app": "bravel-chat", "format": 1, "history": [{"role": "user"}], "shell": "bash", "cwd": directory}))
            with self.assertRaises(ConsoleError):
                store.load("bad", Agent())

    def test_local_app_launch_uses_found_path_and_includes_check(self):
        app = Application("Visual Studio Code", Path("C:/user's tools/Code.exe"), ("vscode",))
        with patch("bravel.programs.installed_applications", return_value=[app]):
            plan = launch_plan("запусти vscode", "powershell")
        self.assertIn("user''s tools", plan.steps[0].command)
        self.assertIn("Get-Process -Name 'Code'", plan.steps[0].check)
        self.assertTrue(read_only(plan.steps[0].check, "powershell"))

    def test_ambiguous_app_launch_does_not_guess(self):
        apps = [Application("Chrome", Path("chrome.exe"), ("browser",)), Application("Firefox", Path("firefox.exe"), ("browser",))]
        with patch("bravel.programs.installed_applications", return_value=apps):
            self.assertEqual(launch_plan("open browser", "cmd").steps, ())

    def test_app_discovery_accepts_existing_executable_only(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "chrome"
            executable.write_text("fixture")
            with patch("bravel.programs.app_paths", return_value=[]), patch("bravel.programs.shutil.which", side_effect=lambda name: str(executable) if name == "chrome" else None):
                apps = installed_applications()
            self.assertTrue(any(app.name == "Google Chrome" for app in apps))

    def test_doctor_network_check_is_opt_in(self):
        from bravel.diagnostics import doctor
        with patch("bravel.diagnostics.Planner.make_plan", return_value=Plan("connected", ())) as model:
            doctor(Settings(api_key="test-secret"), UI("never", io.StringIO()))
            model.assert_not_called()
            doctor(Settings(api_key="test-secret"), UI("never", io.StringIO()), api=True)
            model.assert_called_once()

    def test_offline_rollback_restores_engine_without_touching_user_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            (root / "venv").mkdir(parents=True)
            (root / "venv/version.txt").write_text("old")
            manifest = {"app": "bravel", "root": str(root.resolve()), "profiles": [], "source": "old-source"}
            (root / "bravel-install.json").write_text(json.dumps(manifest))
            (root / "installer.py").write_text("old-installer")
            user_config = Path(directory) / "config.env"
            user_config.write_text("API_KEY=kept-secret")
            with patch("sys.stdout", io.StringIO()):
                install.make_snapshot(root, manifest, "old-version")
                (root / "venv/version.txt").write_text("new")
                (root / "venv/new-module.py").write_text("must vanish")
                install.rollback(root)
            self.assertEqual((root / "venv/version.txt").read_text(), "old")
            self.assertFalse((root / "venv/new-module.py").exists())
            self.assertEqual(user_config.read_text(), "API_KEY=kept-secret")
            self.assertFalse((root / "rollback").exists())

    def test_snapshot_refuses_foreign_rollback_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rollback").mkdir()
            important = root / "rollback/important.txt"
            important.write_text("keep")
            with self.assertRaises((OSError, ValueError, RuntimeError)):
                install.make_snapshot(root, {}, "test")
            self.assertTrue(important.exists())


if __name__ == "__main__":
    unittest.main()
