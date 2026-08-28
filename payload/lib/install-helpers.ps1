# Download and silent-install helpers for a guest with no Store, no winget and no browser.
# curl.exe is one of the few useful binaries the sandbox image does ship.

function global:Get-RemoteFile {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile,
        [string]$Sha256 = ''
    )
    Write-GuestLog "download start $Uri" -Name 'install'
    $partial = "$OutFile.download"
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    try {
        & curl.exe --location --fail --silent --show-error --proto '=https' --tlsv1.2 `
            --output $partial $Uri
        if ($LASTEXITCODE -ne 0) { throw "curl failed with exit code $LASTEXITCODE for $Uri" }
        if (-not (Test-Path -LiteralPath $partial)) { throw "download produced no file: $OutFile" }

        if ($Sha256) {
            $expected = $Sha256 -replace '^sha256:', ''
            if ($expected -notmatch '^[a-fA-F0-9]{64}$') {
                throw "invalid SHA-256 digest for $Uri"
            }
            $actual = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash
            if (-not [StringComparer]::OrdinalIgnoreCase.Equals($actual, $expected)) {
                throw "SHA-256 mismatch for $Uri (expected $expected, got $actual)"
            }
            Write-GuestLog "sha256 verified $actual" -Name 'install'
        }

        Move-Item -LiteralPath $partial -Destination $OutFile -Force
    } finally {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    }
    $bytes = (Get-Item -LiteralPath $OutFile).Length
    Write-GuestLog ("download done {0:N1} MB" -f ($bytes / 1MB)) -Name 'install'
    $bytes
}

function global:Get-GitHubReleaseAsset {
    param(
        [Parameter(Mandatory)][string]$Repo,
        [Parameter(Mandatory)][string]$Pattern
    )
    $json = & curl.exe --location --fail --silent --show-error --proto '=https' --tlsv1.2 `
        --header 'User-Agent: sandbox-mcp' `
        "https://api.github.com/repos/$Repo/releases/latest"
    if ($LASTEXITCODE -ne 0) { throw "curl failed with exit code $LASTEXITCODE querying $Repo" }
    $release = $json | ConvertFrom-Json
    $asset = $release.assets | Where-Object { $_.name -like $Pattern } | Select-Object -First 1
    if (-not $asset) { throw "no asset matching '$Pattern' in $Repo $($release.tag_name)" }
    if ($asset.digest -notmatch '^sha256:[a-fA-F0-9]{64}$') {
        throw "asset $($asset.name) in $Repo $($release.tag_name) has no SHA-256 digest"
    }
    [pscustomobject]@{
        Name    = $asset.name
        Url     = $asset.browser_download_url
        Version = $release.tag_name
        Digest  = $asset.digest
    }
}

function global:Assert-AuthenticodeSignature {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$PublisherPattern
    )
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid') {
        throw "invalid Authenticode signature on $Path ($($signature.Status))"
    }
    $publisher = $signature.SignerCertificate.Subject
    if ($publisher -notmatch $PublisherPattern) {
        throw "unexpected Authenticode publisher on $Path ($publisher)"
    }
    Write-GuestLog "signature verified $publisher" -Name 'install'
}

function global:Wait-ForInstaller {
    # A silent installer that stops to ask something has nobody to answer it, and would otherwise
    # block the agent pump forever. Fail loudly instead.
    param(
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)][string]$Label,
        [int]$TimeoutSeconds = 600
    )
    if (-not $Process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $Process.Kill() } catch { }
        throw "$Label did not finish within $TimeoutSeconds s - it is probably waiting on a prompt"
    }
    $Process.ExitCode
}

function global:Install-Silently {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string[]]$Arguments = @('/S'),
        [int]$TimeoutSeconds = 600
    )
    $label = Split-Path $Path -Leaf
    Write-GuestLog "install start $label $($Arguments -join ' ')" -Name 'install'
    $process = Start-Process -FilePath $Path -ArgumentList $Arguments -PassThru
    $code = Wait-ForInstaller -Process $process -Label $label -TimeoutSeconds $TimeoutSeconds
    Write-GuestLog "install exit $code $label" -Name 'install'
    $code
}

function global:Install-Msi {
    param(
        [Parameter(Mandatory)][string]$Path,
        [string[]]$Properties = @(),
        [int]$TimeoutSeconds = 900
    )
    $label = Split-Path $Path -Leaf
    Write-GuestLog "msi start $label" -Name 'install'
    $arguments = @('/i', "`"$Path`"", '/quiet', '/norestart') + $Properties
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments -PassThru
    $code = Wait-ForInstaller -Process $process -Label $label -TimeoutSeconds $TimeoutSeconds
    Write-GuestLog "msi exit $code $label" -Name 'install'
    $code
}

function global:Update-SessionPath {
    # An installer updates the registry, not this process; re-read PATH so new tools resolve.
    $parts = @(
        [Environment]::GetEnvironmentVariable('PATH', 'Machine'),
        [Environment]::GetEnvironmentVariable('PATH', 'User')
    ) | Where-Object { $_ }
    $env:PATH = ($parts -join ';')
}

function global:Get-RealCommand {
    # Windows ships App Execution Aliases (python.exe, wt.exe) under WindowsApps that only exist to
    # open the Store. There is no Store in the sandbox, so running one hangs or does nothing.
    param([Parameter(Mandatory)][string]$Name)
    Get-Command $Name -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and $_.Source -notmatch '\\WindowsApps\\' } |
        Select-Object -First 1
}

function global:Get-ToolVersion {
    param([Parameter(Mandatory)][string]$Command, [string[]]$Arguments = @('--version'))
    $tool = Get-RealCommand -Name $Command
    if (-not $tool) { return $null }
    try {
        $output = & $tool.Source @Arguments 2>&1 | Select-Object -First 1
        return "$output".Trim()
    } catch {
        return 'installed'
    }
}
