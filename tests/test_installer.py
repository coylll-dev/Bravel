from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install
from bravel.config import Settings, ConsoleError, template
from bravel.setup import configure
from bravel.ui import UI


class Terminal(io.StringIO):
    def isatty(self):
        return True


class InstallerTests(unittest.TestCase):
    def test_installer_leaves_provider_selection_to_wizard_unless_explicit(self):
        python = Path("python")
        self.assertEqual(install.configure_command(python, None), ["python", "-m", "bravel", "configure"])
        self.assertEqual(install.configure_command(python, "gemini"), ["python", "-m", "bravel", "configure", "--provider", "gemini"])
        with patch("install.install") as installer:
            self.assertEqual(install.main(["--no-profile", "--no-configure"]), 0)
            self.assertIsNone(installer.call_args.kwargs["provider"])

    def test_existing_utf16_powershell_profile_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.ps1"
            original = "# мой профиль\nWrite-Host 'привет'\n"
            path.write_text(original, encoding="utf-16")
            install.write_profile(path, install.shell_block(Path(directory) / "app", "powershell"))
            install.write_profile(path, None)
            self.assertEqual(path.read_text(encoding="utf-16"), original)

    @unittest.skipIf(os.name == "nt", "Linux process lifecycle")
    def test_exited_zombie_parent_does_not_block_helper(self):
        with patch("install.Path.exists", return_value=True), patch("install.Path.read_text", return_value="42 (bravel) Z 1 1 1"), patch("install.os.kill") as kill:
            install.wait_for_parent(42)
            kill.assert_not_called()

    def test_profile_install_is_idempotent_and_uninstall_preserves_user_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / ".bashrc"
            original = "# my custom profile\nexport MY_SETTING=1\n"
            profile.write_text(original, encoding="utf-8")
            block = install.shell_block(root / "app space", "bash")
            install.write_profile(profile, block)
            install.write_profile(profile, block)
            self.assertEqual(profile.read_text(encoding="utf-8").count(install.BEGIN), 1)
            install.write_profile(profile, None)
            self.assertEqual(profile.read_text(encoding="utf-8"), original)
            self.assertEqual((root / ".bashrc.bravel.bak").read_text(encoding="utf-8"), original)

    def test_malformed_profile_is_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.ps1"
            original = install.BEGIN + "\nuser text"
            path.write_text(original, encoding="utf-8")
            with self.assertRaises(RuntimeError):
                install.write_profile(path, "new block")
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_uninstaller_refuses_unowned_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "user_files"
            root.mkdir()
            file = root / "important.txt"
            file.write_text("keep", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                install.uninstall(root)
            self.assertTrue(file.exists())
            with self.assertRaises(RuntimeError):
                install.install(root, profiles=[], configure=False)
            self.assertTrue(file.exists())

    def test_uninstaller_removes_only_managed_root_and_profile_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            root.mkdir()
            profile = Path(directory) / ".bashrc"
            original = "# personal\n"
            profile.write_text(original, encoding="utf-8")
            install.write_profile(profile, install.shell_block(root, "bash"))
            (root / "bravel-install.json").write_text(json.dumps({"app": "bravel", "root": str(root.resolve()), "profiles": [{"shell": "bash", "path": str(profile)}]}), encoding="utf-8")
            with patch("sys.stdout", io.StringIO()):
                install.uninstall(root)
            self.assertFalse(root.exists())
            self.assertEqual(profile.read_text(encoding="utf-8"), original)

    def test_shell_paths_are_escaped(self):
        path = Path("C:/user's folder/Bravel")
        self.assertIn("user''s folder", install.shell_block(path, "powershell"))
        self.assertIn("'\"'\"'", install.shell_block(path, "bash"))


class ProviderSettingsTests(unittest.TestCase):
    def test_google_provider_aliases_use_native_gemini(self):
        for alias in ("googleai", "google", "Google-AI-Studio"):
            settings = Settings.from_values({"AI_PROVIDER": alias, "AI_API_KEY": "test-secret"})
            self.assertEqual(settings.provider, "gemini")
            self.assertIn("googleapis.com", settings.base_url)

    def test_interactive_provider_choice_and_reconfigure_preserve_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            with patch("sys.stdin", Terminal()), patch("builtins.input", side_effect=["2", "", "custom-gemini-model"]), patch("getpass.getpass", return_value="test-secret"):
                configure(None, path, UI("never", io.StringIO()))
            with patch("sys.stdin", Terminal()), patch("builtins.input", side_effect=["", "", ""]), patch("getpass.getpass", return_value=""):
                configure(None, path, UI("never", io.StringIO()))
            settings = Settings.load(path)
            self.assertEqual(settings.provider, "gemini")
            self.assertEqual(settings.model, "custom-gemini-model")
            self.assertEqual(settings.api_key, "test-secret")

    def test_provider_defaults_and_native_key_variables(self):
        for provider, key in (("gemini", "GEMINI_API_KEY"), ("openrouter", "OPENROUTER_API_KEY")):
            with patch.dict(os.environ, {"AI_PROVIDER": provider, key: "test-secret"}, clear=True):
                settings = Settings.load(Path("nonexistent-config"))
                self.assertEqual(settings.api_key, "test-secret")
                self.assertEqual(settings.provider, provider)
                self.assertIn("googleapis.com" if provider == "gemini" else "openrouter.ai", settings.base_url)
                self.assertNotIn("test-secret", repr(settings))

    def test_gemini_model_paths_cannot_inject_url_components(self):
        for model in ("../../wrong", "model?key=secret", "foo:generateContent"):
            with patch.dict(os.environ, {"AI_PROVIDER": "gemini", "AI_MODEL": model}, clear=True), self.assertRaises(ConsoleError):
                Settings.load(Path("nonexistent-config"))

    def test_configure_validates_before_replacing_and_hides_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / ".env"
            with patch("sys.stdin", Terminal()), patch("builtins.input", side_effect=["", ""]), patch("getpass.getpass", return_value="gemini-test-secret"), patch("sys.stdout", io.StringIO()) as output:
                configure("gemini", path, UI("never", output))
                self.assertNotIn("gemini-test-secret", output.getvalue())
            self.assertEqual(Settings.load(path).provider, "gemini")
            self.assertEqual(Settings.load(path).api_key, "gemini-test-secret")
            old = path.read_text(encoding="utf-8")
            with patch("sys.stdin", Terminal()), patch("builtins.input", side_effect=["http://remote.example/v1", ""]), patch("getpass.getpass", return_value="other-test-secret"), self.assertRaises(ConsoleError):
                configure("gemini", path, UI("never", io.StringIO()))
            self.assertEqual(path.read_text(encoding="utf-8"), old)

    def test_provider_templates_load(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            for provider in ("openai", "gemini", "openrouter", "compatible"):
                path = Path(directory) / provider
                path.write_text(template(provider), encoding="utf-8")
                self.assertEqual(Settings.load(path).provider, provider)
