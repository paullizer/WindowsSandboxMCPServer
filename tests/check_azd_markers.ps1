<#
Checks that payload\deploy-azd.ps1 turns real azd output into markers the video editor can cut at.

    pwsh -File tests\check_azd_markers.ps1

Host-side only: no sandbox, no Azure, nothing installed. `azd` is replaced by a function that
replays captured output, but `mark` is the real one - SANDBOX_CTL points it at a temporary control
folder, so the marker files, their names, their details and their timestamps are all exercised for
real rather than mocked.

Mock state is $global:mock* on purpose: $script: resolves into the invoked script's scope and
collides with its own variables.
#>

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo 'payload\deploy-azd.ps1'
$failures = New-Object System.Collections.Generic.List[string]

# Captured from a real `azd up`, trimmed to the lines that carry progress.
$global:mockAzdOutput = @(
    'Packaging services (azd package)'
    ''
    '  (' + [char]0x2713 + ') Done: Packaging service web'
    ''
    'Provisioning Azure resources (azd provision)'
    'Provisioning Azure resources can take some time.'
    ''
    'Subscription: Contoso Development (00000000-0000-0000-0000-000000000000)'
    'Location: East US'
    ''
    '  You can view detailed progress in the Azure Portal:'
    '  https://portal.azure.com/#view/HubsExtension/DeploymentDetailsBlade'
    ''
    '  (' + [char]0x2713 + ') Done: Resource group: rg-simplechat-demo'
    '  (' + [char]0x2713 + ') Done: Key Vault: kv-abc123'
    '  (' + [char]0x2713 + ') Done: Storage account: stabc123'
    '  (' + [char]0x2713 + ') Done: Log Analytics workspace: log-abc123'
    '  (' + [char]0x2713 + ') Done: Azure Cognitive Search: srch-abc123'
    '  (' + [char]0x2713 + ') Done: Azure Cache for Redis: redis-abc123'
    '  (' + [char]0x2713 + ') Done: App Service plan: plan-abc123'
    '  (' + [char]0x2713 + ') Done: App Service: app-abc123'
    ''
    'SUCCESS: Your application was provisioned in Azure in 34 minutes 12 seconds.'
    ''
    'Deploying services (azd deploy)'
    ''
    '  (' + [char]0x2713 + ') Done: Deploying service web'
    '  - Endpoint: https://app-abc123.azurewebsites.net/'
    ''
    'SUCCESS: Your up workflow to provision and deploy to Azure completed in 41 minutes 8 seconds.'
)

function global:azd {
    $global:mockAzdArgs = $args
    # A function cannot set a real exit code, so the mock publishes one the way a native command would.
    $global:LASTEXITCODE = $global:mockAzdExitCode
    $global:mockAzdOutput
}

function Get-Markers {
    param([string]$Root)
    $markers = @{}
    Get-ChildItem -Path (Join-Path $Root 'markers') -Filter *.json -ErrorAction SilentlyContinue |
        ForEach-Object { $markers[$_.BaseName] = (Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json) }
    $markers
}

function Invoke-Take {
    param([int]$ExitCode)
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ("azdmark-" + [guid]::NewGuid().ToString('n').Substring(0, 8))
    $project = Join-Path $root 'project'
    New-Item -ItemType Directory -Force -Path $project | Out-Null
    $env:SANDBOX_CTL = $root
    . (Join-Path $repo 'payload\lib\guest.ps1')
    Initialize-Ctl

    $global:mockAzdExitCode = $ExitCode
    $thrown = $null
    try { & $script -Path $project *> $null } catch { $thrown = $_ }
    [pscustomobject]@{ Root = $root; Markers = (Get-Markers $root); Thrown = $thrown }
}

# --- the happy path ----------------------------------------------------------
$take = Invoke-Take -ExitCode 0
$markers = $take.Markers

if ($take.Thrown) { $failures.Add("a successful azd up threw: $($take.Thrown)") }

foreach ($expected in 'azd_up_started', 'azd_packaging', 'azd_provisioning', 'azd_provisioned',
                      'azd_deploying', 'azd_endpoint', 'azd_up_succeeded') {
    if (-not $markers.ContainsKey($expected)) { $failures.Add("missing marker: $expected") }
}
if ($markers.ContainsKey('azd_up_failed')) { $failures.Add('a successful run published azd_up_failed') }

# Ten "Done:" lines: one packaging, eight resources, one deploy. These are the cut points that
# make a forty-minute wait trimmable, so the count matters.
$resourceMarks = $markers.Keys | Where-Object { $_ -like 'azd_resource_*' } | Sort-Object
if ($resourceMarks.Count -ne 10) {
    $failures.Add("expected 10 resource markers, got $($resourceMarks.Count): $($resourceMarks -join ', ')")
}
if ($markers['azd_resource_02'] -and $markers['azd_resource_02'].detail -ne 'Resource group: rg-simplechat-demo') {
    $failures.Add("resource detail not captured: '$($markers['azd_resource_02'].detail)'")
}
if ($markers['azd_up_succeeded'] -and $markers['azd_up_succeeded'].detail -notmatch '^10 resources in \d+ min$') {
    $failures.Add("summary detail malformed: '$($markers['azd_up_succeeded'].detail)'")
}

# The host parses this timestamp back into an epoch; a format change here breaks the manifest.
$stamp = $markers['azd_up_started'].ts
if (-not $stamp -or -not ([datetime]::TryParse($stamp, [ref]([datetime]::MinValue)))) {
    $failures.Add("marker timestamp is not parseable: '$stamp'")
}

if ($global:mockAzdArgs -join ' ' -ne 'up --no-prompt') {
    $failures.Add("azd was not invoked non-interactively: '$($global:mockAzdArgs -join ' ')'")
}

# --- a failed deployment -----------------------------------------------------
$failed = Invoke-Take -ExitCode 3
if (-not $failed.Thrown) {
    $failures.Add('azd up exited 3 but the script reported success')
}
if (-not $failed.Markers.ContainsKey('azd_up_failed')) {
    $failures.Add('a failed deployment published no azd_up_failed marker')
}
if ($failed.Markers.ContainsKey('azd_up_succeeded')) {
    $failures.Add('a failed deployment also published azd_up_succeeded')
}

foreach ($take in @($take, $failed)) {
    Remove-Item -Recurse -Force -LiteralPath $take.Root -ErrorAction SilentlyContinue
}
Remove-Item Env:SANDBOX_CTL -ErrorAction SilentlyContinue

if ($failures.Count) {
    Write-Host 'FAIL'
    $failures | ForEach-Object { Write-Host "  - $_" }
    exit 1
}
Write-Host "OK: deploy-azd.ps1 published $($resourceMarks.Count + 7) markers across the deployment"
exit 0
