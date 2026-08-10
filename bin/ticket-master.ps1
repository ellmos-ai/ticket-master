# ticket-master.ps1 — PowerShell dispatcher for ticket-master
# Usage: .\bin\ticket-master.ps1 [-Provider claude|codex|agy]
param(
    [string]$Provider = $(if ($env:TM_PROVIDER) { $env:TM_PROVIDER } else { "claude" })
)

$ScriptDir  = $PSScriptRoot
$RepoRoot   = Resolve-Path (Join-Path $ScriptDir "..")
& python (Join-Path $ScriptDir "ticket_master.py") --provider $Provider
exit $LASTEXITCODE
