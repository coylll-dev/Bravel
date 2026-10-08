"""Install and uninstall the real package in a temporary, owned directory."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="bravel-lifecycle-") as directory:
        base = Path(directory)
        root = base / "owned-install"
        profile = base / ("profile.ps1" if os.name == "nt" else ".bashrc")
        original = "# user profile must survive\n"
        profile.write_text(original, encoding="utf-8")
        shell = "powershell" if os.name == "nt" else "bash"
        subprocess.run([sys.executable, str(PROJECT / "install.py"), "--prefix", str(root), "--profile", str(profile), "--shell", shell, "--source", str(PROJECT), "--no-configure", "--no-path"], check=True)
        scripts = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
        executable = scripts / ("bravel.exe" if os.name == "nt" else "bravel")
        result = subprocess.run([str(executable), "--version"], check=True, capture_output=True, encoding="utf-8")
        assert result.stdout.strip() == "0.6.1"
        installed_python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(installed_python), "-I", "-c", "import importlib.util; assert importlib.util.find_spec('bravel.chat'); assert importlib.util.find_spec('bravel.desktop') is None; assert importlib.util.find_spec('bravel.bridge') is None"], check=True)
        assert (root / "installer.py").is_file()
        assert "# >>> bravel >>>" in profile.read_text(encoding="utf-8-sig")
        # The maintenance helper inherits the pipe; communicate waits until the
        # asynchronous update has completed, including the Windows launcher exit.
        update = subprocess.run([str(executable), "update"], check=True, capture_output=True, encoding="utf-8", errors="replace")
        assert "Bravel установлен" in update.stdout, update.stdout + update.stderr
        assert profile.read_text(encoding="utf-8-sig").count("# >>> bravel >>>") == 1
        assert (root / "rollback/snapshot.json").is_file()
        marker = root / "venv/added-after-update.txt"
        marker.write_text("must vanish on rollback")
        rollback = subprocess.run([str(executable), "rollback"], check=True, capture_output=True, encoding="utf-8", errors="replace")
        assert "Откат завершён" in rollback.stdout, rollback.stdout + rollback.stderr
        assert not marker.exists()
        assert not (root / "rollback").exists()
        result = subprocess.run([str(executable), "version"], check=True, capture_output=True, encoding="utf-8")
        assert result.stdout.strip() == "0.6.1"
        subprocess.run([str(executable), "uninstall"], check=True)
        deadline = time.monotonic() + 20
        while root.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not root.exists(), "Managed uninstall did not remove its own directory"
        assert profile.read_text(encoding="utf-8-sig") == original
        print("Install/uninstall lifecycle passed")


if __name__ == "__main__":
    main()
