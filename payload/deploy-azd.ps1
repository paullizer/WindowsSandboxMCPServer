# Runs `azd up` and publishes a marker as each milestone goes past, so a deployment that takes the
# better part of an hour has somewhere the video editor can cut. Without markers the editor treats
# the whole step as content and keeps all forty minutes of it.
#
# Inspecting the output line by line means piping it, and azd only draws its animated spinner when
# stdout is a console, so this trades the spinner for plain text. That is the right trade twice
# over: the messages are what carry the progress, and a screen that sits still between them is what
# lets the editor's freeze check agree that a span really is dead air.
#
# Deliberately no elapsed-time heartbeat. A ticking clock would make every frame differ from the
# last, and the pixel check would then veto every trim the manifest proposed.
#
# For the interactive walkthrough use plain `azd up` instead; this script is the unattended path.

[CmdletBinding()]
param(
    [string]$Path = 'C:\simplechat\deployers',
    [string[]]$AzdArgs = @('--no-prompt')
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\guest.ps1')

if (-not (Test-Path -LiteralPath $Path)) {
    throw "No azd project at $Path. Clone the repository before deploying."
}
Set-Location -LiteralPath $Path

# Ordered, one-shot: each of these happens once, and the first line that matches is the moment.
$milestones = @(
    @{ Name = 'azd_packaging'; Pattern = 'Packaging services' }
    @{ Name = 'azd_provisioning'; Pattern = 'Provisioning Azure resources' }
    @{ Name = 'azd_provisioned'; Pattern = 'was provisioned in Azure' }
    @{ Name = 'azd_deploying'; Pattern = 'Deploying services' }
    @{ Name = 'azd_endpoint'; Pattern = 'Endpoint:' }
)

$seen = @{}
$resources = 0
$started = Get-Date
mark 'azd_up_started' -Detail ($AzdArgs -join ' ')

& azd up @AzdArgs 2>&1 | ForEach-Object {
    $line = [string]$_
    Write-Host $line
    foreach ($milestone in $milestones) {
        if (-not $seen[$milestone.Name] -and $line -match $milestone.Pattern) {
            $seen[$milestone.Name] = $true
            mark $milestone.Name
        }
    }
    # Every resource azd finishes is a real progress event, and the natural place to cut a long
    # provisioning wait. Marker files are keyed by name, so these have to be numbered to survive.
    if ($line -match 'Done:\s*(.+?)\s*$') {
        $resources++
        mark ('azd_resource_{0:d2}' -f $resources) -Detail $Matches[1]
    }
}

$code = $LASTEXITCODE
$minutes = [int]((Get-Date) - $started).TotalMinutes

if ($code -ne 0) {
    mark 'azd_up_failed' -Detail "exit $code after $minutes min"
    throw "azd up exited with $code after $minutes minutes"
}

mark 'azd_up_succeeded' -Detail "$resources resources in $minutes min"
Write-GuestLog "azd up finished: $resources resources in $minutes minutes" -Name 'deploy'
