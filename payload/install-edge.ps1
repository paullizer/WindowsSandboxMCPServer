# Installs Microsoft Edge so a deployed site can be opened in the guest, on camera.
# The sandbox image ships no browser at all, so this is storyboard setup work: run it before the
# recording starts, not as a story beat.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\guest.ps1')
. (Join-Path $PSScriptRoot 'lib\install-helpers.ps1')

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$edgeExe = Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'

function Get-EdgeStableMsi {
    # The enterprise feed is the only download that comes with a publisher-declared hash, which is
    # what lets Get-RemoteFile verify what it fetched.
    $json = & curl.exe --location --fail --silent --show-error --proto '=https' --tlsv1.2 `
        --header 'User-Agent: sandbox-mcp' `
        'https://edgeupdates.microsoft.com/api/products?view=enterprise'
    if ($LASTEXITCODE -ne 0) { throw "curl failed with exit code $LASTEXITCODE querying Edge updates" }

    $stable = $json | ConvertFrom-Json | Where-Object { $_.Product -eq 'Stable' }
    if (-not $stable) { throw 'no Stable product in the Edge enterprise feed' }

    $release = $stable.Releases |
        Where-Object { $_.Platform -eq 'Windows' -and $_.Architecture -eq 'x64' } |
        Sort-Object { [version]$_.ProductVersion } -Descending |
        Select-Object -First 1
    if (-not $release) { throw 'no Windows x64 release in the Edge enterprise feed' }

    $artifact = $release.Artifacts | Where-Object { $_.ArtifactName -eq 'msi' } | Select-Object -First 1
    if (-not $artifact) { throw "no msi artifact for Edge $($release.ProductVersion)" }
    if ($artifact.HashAlgorithm -ne 'SHA256' -or $artifact.Hash -notmatch '^[a-fA-F0-9]{64}$') {
        throw "Edge $($release.ProductVersion) msi has no usable SHA-256 hash"
    }

    [pscustomobject]@{
        Version = $release.ProductVersion
        Url     = $artifact.Location
        Sha256  = $artifact.Hash
    }
}

Write-GuestLog '=== Microsoft Edge' -Name 'install'
if (Test-Path -LiteralPath $edgeExe) {
    Write-Host 'already present'
} else {
    $staging = Join-Path $env:TEMP 'prereqs'
    New-Item -ItemType Directory -Force -Path $staging | Out-Null

    $msi = Get-EdgeStableMsi
    Write-GuestLog "Edge stable $($msi.Version)" -Name 'install'
    $file = Join-Path $staging "MicrosoftEdgeEnterpriseX64-$($msi.Version).msi"
    Get-RemoteFile -Uri $msi.Url -OutFile $file -Sha256 $msi.Sha256 | Out-Null
    Assert-AuthenticodeSignature -Path $file -PublisherPattern 'Microsoft Corporation'
    $code = Install-Msi -Path $file
    if ($code -ne 0) { throw "Edge installer exited with $code" }
    if (-not (Test-Path -LiteralPath $edgeExe)) { throw "Edge installed but $edgeExe is missing" }
}

# A first launch that opens onto a welcome tour, a sign-in pitch and a default-browser nag is not
# something anyone wants on camera. These are the documented Edge policies that turn all of it off.
$policy = 'HKLM:\SOFTWARE\Policies\Microsoft\Edge'
New-Item -Path $policy -Force | Out-Null
foreach ($name in 'HideFirstRunExperience', 'SyncDisabled') {
    New-ItemProperty -Path $policy -Name $name -Value 1 -PropertyType DWord -Force | Out-Null
}
foreach ($name in 'BrowserSignin', 'DefaultBrowserSettingEnabled', 'PromotionalTabsEnabled') {
    New-ItemProperty -Path $policy -Name $name -Value 0 -PropertyType DWord -Force | Out-Null
}

$version = (Get-Item -LiteralPath $edgeExe).VersionInfo.ProductVersion
Write-Host ("  edge    {0}" -f $version) -ForegroundColor Green
Write-JsonAtomic -Path (Get-CtlPath 'edge.json') -Value ([ordered]@{
    version = $version
    path    = $edgeExe
})
mark 'edge_installed' $version
