# Compile-check only: extracts the C# from overlay.ps1 and builds it without showing a window.
$ErrorActionPreference = 'Stop'

# The guest compiles this with Windows PowerShell 5.1, and only that host reproduces its rules -
# under pwsh the split assemblies report references (System.ComponentModel.Primitives) that say
# nothing about whether the overlay works in the sandbox. Re-run there rather than report a lie.
if ($PSVersionTable.PSEdition -eq 'Core') {
    $winps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $winps)) {
        Write-Host 'SKIP: Windows PowerShell 5.1 not found, and it is the host the guest uses'
        exit 0
    }
    & $winps -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath
    exit $LASTEXITCODE
}

$repo = Split-Path -Parent $PSScriptRoot
$text = Get-Content -Raw (Join-Path $repo 'payload\lib\overlay.ps1')
$pattern = "(?s)\`$source = @'\r?\n(.*?)\r?\n'@"
$match = [regex]::Match($text, $pattern)
if (-not $match.Success) { Write-Host 'FAIL: could not extract the C# here-string'; exit 1 }

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
try {
    Add-Type -TypeDefinition $match.Groups[1].Value `
        -ReferencedAssemblies 'System.Windows.Forms', 'System.Drawing' -ErrorAction Stop
} catch {
    Write-Host 'FAIL:'
    Write-Host $_.Exception.Message
    if ($_.Exception.InnerException) { Write-Host $_.Exception.InnerException.Message }
    exit 1
}

foreach ($type in 'SandboxMcp.Bead', 'SandboxMcp.TimelineBar', 'SandboxMcp.AgendaCard') {
    if (-not ($type -as [type])) { Write-Host "FAIL: $type missing"; exit 1 }
    Write-Host "OK  $type"
}
Write-Host 'PASS'
