# Installs Notepad++ from a clean machine: resolve the latest release, download, silent install.
# Run it visibly for the camera, or invisibly for setup.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\guest.ps1')
. (Join-Path $PSScriptRoot 'lib\install-helpers.ps1')

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$asset = Get-GitHubReleaseAsset -Repo 'notepad-plus-plus/notepad-plus-plus' -Pattern 'npp.*.Installer.x64.exe'
Write-Host "[notepad++] $($asset.Version) - $($asset.Name)"
mark 'notepadpp_resolved' -Detail $asset.Version

$installer = Join-Path $env:TEMP $asset.Name
$bytes = Get-RemoteFile -Uri $asset.Url -OutFile $installer -Sha256 $asset.Digest
mark 'notepadpp_downloaded' -Detail "$bytes bytes"

$code = Install-Silently -Path $installer -Arguments @('/S')
if ($code -ne 0) { throw "Notepad++ installer exited with code $code" }

$exe = 'C:\Program Files\Notepad++\notepad++.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "installer reported success but $exe is missing" }

Write-Host "[notepad++] installed at $exe"
mark 'notepadpp_installed' -Detail $exe
exit 0
