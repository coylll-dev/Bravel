"""Parse generated shell source without executing it."""
from __future__ import annotations

import json
import os
import subprocess

from .config import ConsoleError


def syntax_errors(shell: str, commands: list[str]) -> list[dict]:
    if not commands or shell == "cmd":
        return []  # CMD has no corresponding parse-only mode.
    from .executor import shell_argv
    try:
        if shell == "powershell":
            script = """$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.Encoding]::UTF8;
$sources=@(ConvertFrom-Json ([Console]::In.ReadToEnd()));
if($sources.Count -eq 0) { throw 'Missing source input' };
$result=@(); $index=0;
foreach($source in $sources) {
    $tokens=$null; $errors=$null;
    $null=[System.Management.Automation.Language.Parser]::ParseInput([string]$source,[ref]$tokens,[ref]$errors);
    foreach($parseError in $errors) { $result += [PSCustomObject]@{index=$index; message=$parseError.Message} }
    $index++;
}; ConvertTo-Json -Compress -InputObject @($result)"""
            # ASCII JSON preserves Unicode via escapes without resetting the
            # PowerShell console input reader (which can discard buffered stdin).
            result = subprocess.run(shell_argv(shell, script), input=json.dumps(commands),
                                    capture_output=True, encoding="utf-8", errors="replace", timeout=15,
                                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            if result.returncode or len(result.stdout) > 32000:
                raise ConsoleError("Не удалось проверить синтаксис плана PowerShell")
            return json.loads(result.stdout)
        if shell == "bash":
            environment = dict(os.environ)
            environment.pop("BASH_ENV", None)
            found = []
            executable = shell_argv(shell, "")[0]
            for index, command in enumerate(commands):
                result = subprocess.run([executable, "--noprofile", "--norc", "-n", "-c", command],
                                        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8",
                                        errors="replace", timeout=15, env=environment)
                if result.returncode:
                    found.append({"index": index, "message": result.stderr[:2000]})
            return found
        return []
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ConsoleError("Не удалось проверить синтаксис плана; команды не выполнены") from exc
