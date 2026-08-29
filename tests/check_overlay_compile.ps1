# Compile-check only: extracts the C# from overlay.ps1 and builds it without showing a window.
$ErrorActionPreference = 'Stop'
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
