# Hidden command pump: runs jobs the host drops into C:\ctl\inbox and reports results.
# Started by bootstrap.ps1; never visible on camera.

$ErrorActionPreference = 'Stop'
$bootLog = Join-Path $(if ($env:SANDBOX_CTL) { $env:SANDBOX_CTL } else { 'C:\ctl' }) 'agent-boot.log'

try {
    "agent starting pid=$PID $(Get-Date -Format o)" | Set-Content -LiteralPath $bootLog
    . (Join-Path $PSScriptRoot 'guest.ps1')
    Initialize-Ctl
    "agent initialised, ctl=$global:CtlRoot" | Add-Content -LiteralPath $bootLog
} catch {
    "FATAL during startup: $_`n$($_.ScriptStackTrace)" | Add-Content -LiteralPath $bootLog
    throw
}

$inbox = Get-CtlPath 'inbox'
$running = Get-CtlPath 'running'
$outbox = Get-CtlPath 'outbox'
$agentStatus = Get-CtlPath 'agent.json'

function Read-Tail {
    param([string]$Path, [int]$Lines = 40)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    (Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction SilentlyContinue) -join "`n"
}

function Invoke-Job {
    param([System.IO.FileInfo]$Script)
    $id = [System.IO.Path]::GetFileNameWithoutExtension($Script.Name)
    $target = Join-Path $running $Script.Name
    Move-Item -LiteralPath $Script.FullName -Destination $target -Force
    $out = Join-Path $running "$id.out"
    $err = Join-Path $running "$id.err"
    $started = (Get-Date).ToString('o')

    Write-JsonAtomic -Path $agentStatus -Value ([ordered]@{
        alive = $true; pid = $PID; running = $id; ts = $started
    })

    $exitCode = $null
    $failure = ''
    try {
        # The agent starts at logon, so its PATH predates anything the run installs. Re-read it per
        # job or hidden commands cannot see tools the visible console can.
        $machinePath = [Environment]::GetEnvironmentVariable('PATH', 'Machine')
        $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
        $env:PATH = (@($machinePath, $userPath) | Where-Object { $_ }) -join ';'

        # -NoNewWindow rather than -WindowStyle: the latter is ignored once output is redirected,
        # and would flash a console on camera. The agent's own console is already hidden.
        $process = Start-Process -FilePath 'powershell.exe' -PassThru -Wait -NoNewWindow `
            -ArgumentList '-ExecutionPolicy', 'Bypass', '-NoProfile', '-File', $target `
            -RedirectStandardOutput $out -RedirectStandardError $err
        $exitCode = $process.ExitCode
    } catch {
        $failure = "$_"
    }

    Write-JsonAtomic -Path (Join-Path $outbox "$id.json") -Value ([ordered]@{
        id          = $id
        exit_code   = $exitCode
        started     = $started
        finished    = (Get-Date).ToString('o')
        stdout_tail = Read-Tail $out
        stderr_tail = Read-Tail $err
        failure     = $failure
    })
}

while ($true) {
    try {
        Write-JsonAtomic -Path $agentStatus -Value ([ordered]@{
            alive = $true; pid = $PID; running = $null; ts = (Get-Date).ToString('o')
        })
        foreach ($script in @(Get-ChildItem -LiteralPath $inbox -Filter '*.ps1' -ErrorAction SilentlyContinue | Sort-Object Name)) {
            Invoke-Job -Script $script
        }
    } catch {
        # Nothing in here may terminate the pump - a dead agent takes the whole session with it.
        try {
            Write-JsonAtomic -Path $agentStatus -Value ([ordered]@{
                alive = $true; pid = $PID; error = "$_"; ts = (Get-Date).ToString('o')
            })
        } catch { }
    }
    Start-Sleep -Milliseconds 400
}
