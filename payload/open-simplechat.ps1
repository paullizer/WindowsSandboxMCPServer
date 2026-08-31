# Opens the deployed SimpleChat site in the guest's browser, as the closing shot of a deployment.
# Needs install-edge.ps1 to have run first - the sandbox image has no browser of its own.

param(
    [string]$ProjectDir = 'C:\simplechat\deployers',
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\guest.ps1')

$edgeExe = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'

Push-Location -LiteralPath $ProjectDir
try {
    $raw = & azd env get-values
    if ($LASTEXITCODE -ne 0) { throw "azd env get-values failed with $LASTEXITCODE" }
} finally {
    Pop-Location
}

$values = [ordered]@{}
foreach ($line in $raw) {
    if ("$line" -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"?(.*?)"?\s*$') {
        $values[$Matches[1]] = $Matches[2]
    }
}

$endpoints = @($values.GetEnumerator() | Where-Object { $_.Value -match '^https://' })
if (-not $endpoints) { throw "no https endpoint in the azd environment under $ProjectDir" }

# Several services publish an endpoint; the one worth opening is the web front end. Prefer a key
# that names it, and fall back to the App Service host, which only the site itself has.
$site = $endpoints |
    Where-Object { $_.Key -match 'WEB|SITE|FRONTEND|APP_URI|APP_URL' } |
    Select-Object -First 1
if (-not $site) {
    $site = $endpoints | Where-Object { $_.Value -match 'azurewebsites\.net' } | Select-Object -First 1
}
# SimpleChat publishes the App Service NAME rather than a URL, so build one from it.
if (-not $site) {
    $named = @($values.GetEnumerator() |
        Where-Object { $_.Key -match 'webService|WEB_SERVICE|APP_SERVICE_NAME' -and $_.Value })[0]
    if ($named) {
        $site = [pscustomobject]@{ Key = $named.Key; Value = "https://$($named.Value).azurewebsites.net" }
    }
}
if (-not $site) { throw 'no web endpoint in the azd environment' }
$url = $site.Value.TrimEnd('/')

Write-Host ''
Write-Host 'SimpleChat is deployed at' -ForegroundColor Cyan
Write-Host "  $url" -ForegroundColor Green
Write-Host ''
$rg = $values['AZURE_RESOURCE_GROUP']
if (-not $rg) { $rg = $values['var_rgName'] }
if ($rg) {
    Write-Host ("  resource group  {0}" -f $rg)
}
if ($values['AZURE_LOCATION']) {
    Write-Host ("  location        {0}" -f $values['AZURE_LOCATION'])
}
Write-Host ("  endpoints       {0}" -f $endpoints.Count)

if ($NoLaunch) { return }
if (-not (Test-Path -LiteralPath $edgeExe)) {
    throw "no browser in the guest; run C:\payload\install-edge.ps1 before the recording starts"
}

Add-Type -AssemblyName System.Windows.Forms
$area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
# Framed above the on-camera timeline bar, the same strip the console is already kept clear of.
$height = [Math]::Max(400, $area.Height - (Get-OverlayBarHeight))

Write-GuestLog "opening $url" -Name 'progress'
Start-Process -FilePath $edgeExe -ArgumentList @(
    '--no-first-run',
    '--new-window',
    "--window-position=$($area.Left),$($area.Top)",
    "--window-size=$($area.Width),$height",
    $url
) | Out-Null

mark 'simplechat_opened' $url
