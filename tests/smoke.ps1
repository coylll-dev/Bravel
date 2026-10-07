$ErrorActionPreference = 'Stop'
$global:requests = [System.Collections.Generic.List[string]]::new()
function global:bravel {
    $global:requests.Add(($args -join '|'))
    $global:LASTEXITCODE = 0
    if ($args[0] -eq 'ask') {
        $fileIndex = [Array]::IndexOf($args, '--command-file')
        [IO.File]::WriteAllText($args[$fileIndex + 1], "Set-Location -LiteralPath '$($env:TEMP.Replace("'", "''"))'")
    }
}
$originalHandler = $ExecutionContext.InvokeCommand.CommandNotFoundAction
$originalLocation = Get-Location
$expectedLocation = (Get-Item -LiteralPath $env:TEMP).FullName.TrimEnd([char[]]'\/')
. "$PSScriptRoot/../bravel/integrations/bravel.ps1"
try {
    cdm 'argument with space' '$(literal)'
    if ($global:requests.Count -ne 1 -or $global:requests[0] -notlike '*cdm*') { throw 'Unknown command did not reach Bravel' }
    if ($global:requests[0] -notlike '*argument with space*' -or $global:requests[0] -notlike '*$(literal)*') { throw 'Arguments changed' }
    ai 'change directory'
    $actualLocation = (Get-Item -LiteralPath (Get-Location).Path).FullName.TrimEnd([char[]]'\/')
    if ($actualLocation -ne $expectedLocation) { throw "Parent working directory was not preserved: expected=$expectedLocation actual=$actualLocation" }
    Disable-Bravel
    if ($ExecutionContext.InvokeCommand.CommandNotFoundAction -ne $originalHandler) { throw 'Handler was not restored' }
    Write-Host 'PowerShell integration smoke test passed'
} finally {
    Set-Location -LiteralPath $originalLocation.Path
}
