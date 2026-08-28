# Clones simplechat into the guest. Run before authenticating - it needs no Azure access.

param(
    [Parameter(Mandatory)][string]$Repo,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{40}$')][string]$Commit,
    [string]$Dest = 'C:\simplechat'
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib\guest.ps1')
. (Join-Path $PSScriptRoot 'lib\install-helpers.ps1')
Update-SessionPath

if (Test-Path -LiteralPath (Join-Path $Dest '.git')) {
    Write-Host "Already cloned at $Dest"
    $actual = (& git.exe -C $Dest rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "could not read the existing checkout at $Dest" }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($actual, $Commit)) {
        throw "existing checkout is at $actual, expected $Commit"
    }
} else {
    Write-Host "Fetching $Repo @ $Commit -> $Dest" -ForegroundColor Cyan
    & git.exe init --quiet $Dest
    if ($LASTEXITCODE -ne 0) { throw "git init failed with $LASTEXITCODE" }
    & git.exe -C $Dest remote add origin $Repo
    if ($LASTEXITCODE -ne 0) { throw "git remote add failed with $LASTEXITCODE" }
    & git.exe -C $Dest fetch --quiet --depth 1 origin $Commit
    if ($LASTEXITCODE -ne 0) { throw "git fetch failed with $LASTEXITCODE" }
    & git.exe -C $Dest checkout --quiet --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw "git checkout failed with $LASTEXITCODE" }
}

$deployers = Join-Path $Dest 'deployers'
if (-not (Test-Path -LiteralPath (Join-Path $deployers 'azure.yaml'))) {
    throw "azure.yaml not found under $deployers - the repo layout is not what the deploy expects"
}

$commit = (& git.exe -C $Dest rev-parse HEAD).Trim()
Write-Host ""
Write-Host "simplechat @ $commit" -ForegroundColor Green
Write-Host "azd project: $deployers"

mark 'simplechat_cloned' -Detail $commit
exit 0
