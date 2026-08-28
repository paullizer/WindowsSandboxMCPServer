# LogonCommand entry point. Runs once, in the guest, before anything else.
# It is deliberately short-lived: its own console is not interactive (stdin hits EOF at logon and
# PowerShell exits despite -NoExit), so the terminal seen on camera is a separate console it starts.

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot 'lib\guest.ps1')

Initialize-Ctl
Write-BootStatus -Phase 'booting'

$agentScript = Join-Path $PSScriptRoot 'lib\agent.ps1'
$agentLog = Get-CtlPath 'agent-stdout.log'

function Start-Hidden {
    param([Parameter(Mandatory)][string]$Script, [Parameter(Mandatory)][string]$Log)
    # Not Start-Process: ShellExecute is unreliable this early in logon, and this captures the
    # child's own startup output instead of losing it with the window.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'cmd.exe'
    $psi.Arguments = "/c powershell.exe -ExecutionPolicy Bypass -NoProfile -File `"$Script`" >> `"$Log`" 2>&1"
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    [System.Diagnostics.Process]::Start($psi)
}

# Starting children during logon is racy, so confirm and retry rather than leaving the host to
# discover later that nothing is listening.
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $agent = Start-Hidden -Script $agentScript -Log $agentLog
    Write-BootStatus -AgentPid $agent.Id
    Start-Sleep -Seconds 3
    if (Test-Path -LiteralPath (Get-CtlPath 'agent.json')) {
        Write-BootStatus -AgentStarted $true
        break
    }
    Write-BootStatus -Note "agent attempt $attempt produced no heartbeat"
}

$console = Start-Process -FilePath 'powershell.exe' -PassThru -ArgumentList `
    '-ExecutionPolicy', 'Bypass', '-NoProfile', '-NoExit', '-File', (Join-Path $PSScriptRoot 'lib\console.ps1')
Write-BootStatus -ConsolePid $console.Id

$readyMarker = Join-Path (Get-CtlPath 'markers') 'console_ready.json'
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $readyMarker)) {
    Start-Sleep -Milliseconds 400
}

Write-BootStatus -Phase 'ready'
mark 'desktop_ready'
