# The visible terminal, and the only thing the camera sees at boot.
# Started by bootstrap.ps1 with -NoExit so it stays interactive with a real console stdin.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'guest.ps1')
. (Join-Path $PSScriptRoot 'install-helpers.ps1')

Initialize-Ctl
$geometry = Set-ConsoleForVideo -FontHeight 22
$Host.UI.RawUI.WindowTitle = 'Windows Sandbox'
Set-Location -Path 'C:\'

Start-Transcript -Path (Get-CtlPath 'transcript.log') -Append | Out-Null

# Publishes the exit code of every command typed here, so the host can await it instead of guessing.
$global:GuestPromptIndex = 0
function global:prompt {
    $succeeded = $?
    $code = if ($null -ne $LASTEXITCODE) { [int]$LASTEXITCODE } else { 0 }
    $global:GuestPromptIndex++
    Write-GuestStatus -Phase 'prompt' -PromptIndex $global:GuestPromptIndex `
        -LastExitCode $code -LastSucceeded $succeeded
    "PS $($ExecutionContext.SessionState.Path.CurrentLocation)> "
}

Write-Host ''
Write-Host '  Windows Sandbox is ready.' -ForegroundColor Cyan
Write-Host ''

Write-GuestStatus -Phase 'ready'
mark 'console_ready' -Detail "pid $PID; $geometry"
