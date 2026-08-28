# Installs everything simplechat's azd deployment needs, on a machine that has none of it.
# The sandbox image has no Store, no winget and no browser, so every tool is fetched directly.
# Prereqs per deployers/bicep/README.md: Azure CLI 2.50+, azd 1.5+, Python 3.12, PowerShell 7.
# Git is needed to clone the repo in the first place.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\guest.ps1')
. (Join-Path $PSScriptRoot 'lib\install-helpers.ps1')

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$staging = Join-Path $env:TEMP 'prereqs'
New-Item -ItemType Directory -Force -Path $staging | Out-Null

function Write-Step {
    param([int]$Number, [string]$Title)
    Write-Host ''
    Write-GuestLog "=== [$Number/5] $Title" -Name 'install'
}

# --- 1. Git ------------------------------------------------------------------
Write-Step 1 'Git for Windows'
if (Get-RealCommand 'git.exe') {
    Write-Host 'already present'
} else {
    $asset = Get-GitHubReleaseAsset -Repo 'git-for-windows/git' -Pattern 'Git-*-64-bit.exe'
    $file = Join-Path $staging $asset.Name
    Get-RemoteFile -Uri $asset.Url -OutFile $file -Sha256 $asset.Digest | Out-Null
    $code = Install-Silently -Path $file -Arguments @(
        '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES'
    )
    if ($code -ne 0) { throw "Git installer exited with $code" }
}
Update-SessionPath
mark 'prereq_git'

# --- 2. Python 3.12 ----------------------------------------------------------
# python.org ships a WiX burn bundle that wedges indefinitely under /quiet in the sandbox, with or
# without elevation. A standalone build is just an archive, so there is no installer to hang.
Write-Step 2 'Python 3.12 (standalone build)'
$pythonHome = 'C:\Python312'
if (Test-Path -LiteralPath (Join-Path $pythonHome 'python.exe')) {
    Write-Host 'already present'
} else {
    $asset = Get-GitHubReleaseAsset -Repo 'astral-sh/python-build-standalone' `
        -Pattern 'cpython-3.12.*-x86_64-pc-windows-msvc-install_only.tar.gz'
    $archive = Join-Path $staging 'python-3.12-standalone.tar.gz'
    Get-RemoteFile -Uri $asset.Url -OutFile $archive -Sha256 $asset.Digest | Out-Null

    New-Item -ItemType Directory -Force -Path $pythonHome | Out-Null
    Write-GuestLog "extracting to $pythonHome" -Name 'install'
    & tar.exe -xzf $archive -C $pythonHome --strip-components=1
    if ($LASTEXITCODE -ne 0) { throw "tar extraction failed with $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath (Join-Path $pythonHome 'python.exe'))) {
        throw "extraction produced no python.exe under $pythonHome"
    }

    # The azd hooks invoke a bare `python`, so it has to be on PATH for later processes too.
    $userPath = [Environment]::GetEnvironmentVariable('PATH', 'User')
    if ($userPath -notlike "*$pythonHome*") {
        [Environment]::SetEnvironmentVariable(
            'PATH', "$pythonHome;$pythonHome\Scripts;$userPath", 'User')
    }
    Write-GuestLog 'python extracted and added to PATH' -Name 'install'
}
Update-SessionPath

& "$pythonHome\python.exe" -m ensurepip --default-pip 2>&1 | Out-Null
mark 'prereq_python'

# --- 3. PowerShell 7 ---------------------------------------------------------
Write-Step 3 'PowerShell 7'
if (Get-RealCommand 'pwsh.exe') {
    Write-Host 'already present'
} else {
    $asset = Get-GitHubReleaseAsset -Repo 'PowerShell/PowerShell' -Pattern 'PowerShell-*-win-x64.msi'
    $file = Join-Path $staging $asset.Name
    Get-RemoteFile -Uri $asset.Url -OutFile $file -Sha256 $asset.Digest | Out-Null
    $code = Install-Msi -Path $file -Properties @('ADD_PATH=1')
    if ($code -ne 0) { throw "PowerShell 7 installer exited with $code" }
}
Update-SessionPath
mark 'prereq_pwsh'

# --- 4. Azure CLI ------------------------------------------------------------
Write-Step 4 'Azure CLI'
if (Get-RealCommand 'az.cmd') {
    Write-Host 'already present'
} else {
    $file = Join-Path $staging 'azure-cli.msi'
    Get-RemoteFile -Uri 'https://aka.ms/installazurecliwindowsx64' -OutFile $file | Out-Null
    Assert-AuthenticodeSignature -Path $file -PublisherPattern 'Microsoft Corporation'
    $code = Install-Msi -Path $file
    if ($code -ne 0) { throw "Azure CLI installer exited with $code" }
}
Update-SessionPath
# Otherwise `az login` stops on a subscription picker that nothing can answer unattended.
& az.cmd config set core.login_experience_v2=off --only-show-errors 2>&1 | Out-Null
& az.cmd config set core.collect_telemetry=false --only-show-errors 2>&1 | Out-Null
mark 'prereq_azurecli'

# --- 5. Azure Developer CLI --------------------------------------------------
Write-Step 5 'Azure Developer CLI (azd)'
if (Get-RealCommand 'azd.exe') {
    Write-Host 'already present'
} else {
    $asset = Get-GitHubReleaseAsset -Repo 'Azure/azure-dev' -Pattern 'azd-windows-amd64.msi'
    $file = Join-Path $staging $asset.Name
    Get-RemoteFile -Uri $asset.Url -OutFile $file -Sha256 $asset.Digest | Out-Null
    Assert-AuthenticodeSignature -Path $file -PublisherPattern 'Microsoft Corporation'
    $code = Install-Msi -Path $file
    if ($code -ne 0) { throw "azd installer exited with $code" }
}
Update-SessionPath
mark 'prereq_azd'

# --- verify ------------------------------------------------------------------
Write-Host ''
Write-Host 'Verifying prerequisites' -ForegroundColor Cyan
$versions = [ordered]@{
    git    = Get-ToolVersion -Command 'git.exe'
    python = Get-ToolVersion -Command 'python.exe'
    pip    = Get-ToolVersion -Command 'python.exe' -Arguments @('-m', 'pip', '--version')
    pwsh   = Get-ToolVersion -Command 'pwsh.exe' -Arguments @('-NoProfile', '-Command', '$PSVersionTable.PSVersion.ToString()')
    az     = Get-ToolVersion -Command 'az.cmd' -Arguments @('--version')
    azd    = Get-ToolVersion -Command 'azd.exe' -Arguments @('version')
}
$missing = @()
foreach ($name in $versions.Keys) {
    if ($versions[$name]) {
        Write-Host ("  {0,-7} {1}" -f $name, $versions[$name]) -ForegroundColor Green
    } else {
        Write-Host ("  {0,-7} MISSING" -f $name) -ForegroundColor Red
        $missing += $name
    }
}
Write-JsonAtomic -Path (Get-CtlPath 'prereqs.json') -Value $versions

if ($missing.Count -gt 0) {
    mark 'prereqs_failed' -Detail ($missing -join ',')
    throw "Missing after install: $($missing -join ', ')"
}

Write-Host ''
Write-Host 'All prerequisites installed.' -ForegroundColor Green
mark 'prereqs_installed' -Detail (($versions.Keys | ForEach-Object { "$_=$($versions[$_])" }) -join '; ')
exit 0
