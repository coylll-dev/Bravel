# Dot-source this file in PowerShell. No replacement shell or modified prompt.
if ($global:AIConsoleLoaded) { return }
$global:AIConsoleLoaded = $true

function global:Invoke-AIConsole {
    param([Parameter(Mandatory)][string]$Request, [switch]$Fix)
    if ($global:AIConsoleBusy) { return }
    $global:AIConsoleBusy = $true
    $approvedFile = $null
    try {
        $approvedFile = [IO.Path]::GetTempFileName()
        $mode = if ($Fix) { 'fix' } else { 'ask' }
        # Keep human output on the console; capturing native stderr would cause
        # PowerShell to render the entire UI as errors and lose terminal colors.
        & bravel $mode --shell powershell --command-file $approvedFile -- $Request
        $approved = [IO.File]::ReadAllText($approvedFile, [Text.Encoding]::UTF8)
        if ($LASTEXITCODE -eq 0 -and $approved) {
            # Only the previously displayed and explicitly confirmed script.
            . ([scriptblock]::Create($approved))
        }
    } finally {
        $global:AIConsoleBusy = $false
        if ($approvedFile) { [IO.File]::Delete($approvedFile) }
    }
}

function global:ai {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Words)
    Invoke-AIConsole -Request ($Words -join ' ')
}

$global:AIConsolePreviousNotFound = $ExecutionContext.InvokeCommand.CommandNotFoundAction
$ExecutionContext.InvokeCommand.CommandNotFoundAction = {
    param($commandName, $eventArgs)
    if ($global:AIConsoleBusy -or -not [Environment]::UserInteractive) { return }
    # A custom handler is invoked with the original command arguments. Preserve
    # them as literal PowerShell strings; never evaluate the failed input twice.
    $eventArgs.CommandScriptBlock = {
        $invocation = $MyInvocation.InvocationName
        $quoted = @($args | ForEach-Object { "'" + ([string]$_).Replace("'", "''") + "'" })
        $request = (@($invocation) + $quoted) -join ' '
        Invoke-AIConsole -Request $request -Fix
    }
    $eventArgs.StopSearch = $true
}

if (Get-Module -ListAvailable PSReadLine) {
    Import-Module PSReadLine
    $global:AIConsolePreviousEnter = Get-PSReadLineKeyHandler -Bound | Where-Object Key -eq 'Enter' | Select-Object -First 1
    Set-PSReadLineKeyHandler -Key Enter -BriefDescription AIConsole -LongDescription 'Send # prompts to Bravel' -ScriptBlock {
        param($key, $arg)
        $line = $null
        $cursor = 0
        [Microsoft.PowerShell.PSConsoleReadLine]::GetBufferState([ref]$line, [ref]$cursor)
        if ($line.TrimStart().StartsWith('#') -and -not $line.Contains("`n")) {
            $request = $line.TrimStart().Substring(1).Trim()
            if ($request) {
                $escaped = $request.Replace("'", "''")
                [Microsoft.PowerShell.PSConsoleReadLine]::RevertLine()
                [Microsoft.PowerShell.PSConsoleReadLine]::Insert("ai '$escaped'")
            }
        }
        [Microsoft.PowerShell.PSConsoleReadLine]::AcceptLine()
    }
}

function global:Disable-Bravel {
    $ExecutionContext.InvokeCommand.CommandNotFoundAction = $global:AIConsolePreviousNotFound
    if ($global:AIConsolePreviousEnter -and (Get-Module PSReadLine)) {
        if ($global:AIConsolePreviousEnter.ScriptBlock) {
            Set-PSReadLineKeyHandler -Key Enter -ScriptBlock $global:AIConsolePreviousEnter.ScriptBlock
        } else {
            Set-PSReadLineKeyHandler -Key Enter -Function $global:AIConsolePreviousEnter.Function
        }
    }
    $global:AIConsoleLoaded = $false
    Remove-Item Function:\ai, Function:\Invoke-AIConsole, Function:\Disable-Bravel -ErrorAction SilentlyContinue
}

Write-Host '  ◆ Bravel подключён · # запрос · ai запрос · Disable-Bravel' -ForegroundColor Cyan
