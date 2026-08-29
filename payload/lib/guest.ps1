# Shared guest primitives. Dot-sourced by bootstrap.ps1, agent.ps1 and every job script.
# Functions are declared global so they survive into the interactive prompt that -NoExit leaves.

# Overridable so these scripts can be exercised on the host without a sandbox.
$global:CtlRoot = if ($env:SANDBOX_CTL) { $env:SANDBOX_CTL } else { 'C:\ctl' }

function global:Get-CtlPath {
    param([Parameter(Mandatory)][string]$Name)
    Join-Path $global:CtlRoot $Name
}

function global:Get-OverlayBarHeight {
    # The strip the on-camera timeline bar owns at the bottom of the desktop. Shared so the console
    # is framed above it whether or not a storyboard is driving the overlay - a run that shifted
    # the terminal the moment an agenda appeared would look like a glitch.
    88
}

function global:Initialize-Ctl {
    foreach ($dir in 'inbox', 'running', 'outbox', 'markers') {
        New-Item -ItemType Directory -Force -Path (Get-CtlPath $dir) | Out-Null
    }
}

function global:Write-TextNoBom {
    # Set-Content -Encoding UTF8 emits a BOM on Windows PowerShell 5.1, and azd's JSON parser
    # rejects it outright with "invalid character looking for beginning of value".
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function global:Write-JsonAtomic {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Value)
    # The host polls these files, so publish by rename rather than letting it read a partial write.
    # Move-Item -Force is not a true atomic replace on the sandbox's VSMB mapped folder and
    # intermittently fails with "file already exists", so fall back rather than throw.
    $json = $Value | ConvertTo-Json -Depth 6
    $temp = "$Path.tmp"
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            Write-TextNoBom -Path $temp -Text $json
            try {
                Move-Item -LiteralPath $temp -Destination $Path -Force -ErrorAction Stop
            } catch {
                Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
                Move-Item -LiteralPath $temp -Destination $Path -Force -ErrorAction Stop
            }
            return
        } catch {
            Start-Sleep -Milliseconds (75 * $attempt)
        }
    }
    # A torn read the host will retry beats a dead writer.
    try { Write-TextNoBom -Path $Path -Text $json } catch { }
}

function global:Write-GuestStatus {
    param(
        [string]$Phase,
        [Nullable[int]]$PromptIndex,
        [Nullable[int]]$LastExitCode,
        [Nullable[bool]]$LastSucceeded
    )
    if (-not $global:GuestStatus) {
        $global:GuestStatus = [ordered]@{
            phase          = 'booting'
            prompt_index   = 0
            last_exit_code = $null
            last_succeeded = $null
            console_pid    = $PID
            ts             = $null
        }
    }
    if ($Phase) { $global:GuestStatus.phase = $Phase }
    if ($null -ne $PromptIndex) { $global:GuestStatus.prompt_index = $PromptIndex }
    if ($null -ne $LastExitCode) { $global:GuestStatus.last_exit_code = $LastExitCode }
    if ($null -ne $LastSucceeded) { $global:GuestStatus.last_succeeded = $LastSucceeded }
    $global:GuestStatus.ts = (Get-Date).ToString('o')
    Write-JsonAtomic -Path (Get-CtlPath 'status.json') -Value $global:GuestStatus
}

function global:Write-GuestLog {
    # The agent pump is serial, so a long job blocks every other probe. Streaming progress to the
    # mapped folder is the only way the host can watch a slow step without interrupting it.
    param([Parameter(Mandatory)][string]$Message, [string]$Name = 'progress')
    $line = '{0} {1}' -f (Get-Date -Format 'HH:mm:ss'), $Message
    try { Add-Content -LiteralPath (Get-CtlPath "$Name.log") -Value $line -ErrorAction Stop } catch { }
    Write-Host $line
}

function global:mark {
    param([Parameter(Mandatory)][string]$Name, [string]$Detail = '')
    $safe = ($Name -replace '[^A-Za-z0-9._-]', '_')
    Write-JsonAtomic -Path (Join-Path (Get-CtlPath 'markers') "$safe.json") -Value ([ordered]@{
        name   = $Name
        detail = $Detail
        ts     = (Get-Date).ToString('o')
    })
    Write-Host "[mark] $Name $Detail"
}

function global:Write-BootStatus {
    param(
        [string]$Phase,
        [Nullable[int]]$AgentPid,
        [Nullable[int]]$ConsolePid,
        [Nullable[bool]]$AgentStarted,
        [string]$Note
    )
    if (-not $global:GuestBoot) {
        $global:GuestBoot = [ordered]@{
            phase         = 'booting'
            agent_pid     = $null
            console_pid   = $null
            agent_started = $false
            note          = ''
            ts            = $null
        }
    }
    if ($Phase) { $global:GuestBoot.phase = $Phase }
    if ($null -ne $AgentPid) { $global:GuestBoot.agent_pid = $AgentPid }
    if ($null -ne $ConsolePid) { $global:GuestBoot.console_pid = $ConsolePid }
    if ($null -ne $AgentStarted) { $global:GuestBoot.agent_started = $AgentStarted }
    if ($Note) { $global:GuestBoot.note = $Note }
    $global:GuestBoot.ts = (Get-Date).ToString('o')
    Write-JsonAtomic -Path (Get-CtlPath 'boot.json') -Value $global:GuestBoot
}

function global:Show-GuestConsole {
    param([int]$ConsolePid = 0)
    if (-not ('SandboxMcp.Focus' -as [type])) {
        Add-Type -Namespace SandboxMcp -Name Focus -MemberDefinition @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, IntPtr lpdwProcessId);
[DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr hWnd, System.Text.StringBuilder text, int count);
[DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
'@
    }
    $target = [IntPtr]::Zero
    if ($ConsolePid -gt 0) {
        $process = Get-Process -Id $ConsolePid -ErrorAction SilentlyContinue
        if ($process -and $process.MainWindowHandle -ne [IntPtr]::Zero) { $target = $process.MainWindowHandle }
    }
    if ($target -eq [IntPtr]::Zero) {
        $candidates = @(Get-Process -Name 'powershell', 'cmd', 'WindowsTerminal' -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero })
        Write-Host "[focus] pid $ConsolePid has no window; candidates: $(($candidates | ForEach-Object { $_.ProcessName + ':' + $_.Id }) -join ', ')"
        if ($candidates.Count -eq 0) { throw "no console window found in the guest" }
        $target = $candidates[0].MainWindowHandle
    }

    [void][SandboxMcp.Focus]::ShowWindow($target, 9)
    $current = [SandboxMcp.Focus]::GetCurrentThreadId()
    $foreground = [SandboxMcp.Focus]::GetForegroundWindow()
    $threads = @([SandboxMcp.Focus]::GetWindowThreadProcessId($target, [IntPtr]::Zero))
    if ($foreground -ne [IntPtr]::Zero) {
        $threads += [SandboxMcp.Focus]::GetWindowThreadProcessId($foreground, [IntPtr]::Zero)
    }
    # A background process cannot just take the foreground; borrow the owning input queues.
    $attached = @($threads | Where-Object { $_ -ne 0 -and $_ -ne $current } | Select-Object -Unique)
    foreach ($thread in $attached) { [void][SandboxMcp.Focus]::AttachThreadInput($current, $thread, $true) }
    try {
        [void][SandboxMcp.Focus]::BringWindowToTop($target)
        [void][SandboxMcp.Focus]::SetForegroundWindow($target)
    } finally {
        foreach ($thread in $attached) { [void][SandboxMcp.Focus]::AttachThreadInput($current, $thread, $false) }
    }
    Start-Sleep -Milliseconds 250

    if ([SandboxMcp.Focus]::GetForegroundWindow() -ne $target) {
        try { [void](New-Object -ComObject WScript.Shell).AppActivate($ConsolePid) } catch { }
        Start-Sleep -Milliseconds 250
    }

    $now = [SandboxMcp.Focus]::GetForegroundWindow()
    $title = New-Object System.Text.StringBuilder 256
    [void][SandboxMcp.Focus]::GetWindowTextW($now, $title, 256)
    Write-Host "[focus] foreground is '$($title.ToString())'"
    $now -eq $target
}

function global:Get-GuestScreen {
    # Start-Transcript only flushes when a command finishes, so it cannot show a device code or an
    # interactive prompt while the command is still running. Reading the console screen buffer can:
    # attach to the visible console, read the rows it is currently showing, detach.
    param(
        [Parameter(Mandatory)][int]$ConsolePid,
        [int]$Lines = 0,
        [string]$OutFile = 'C:\ctl\screen.txt'
    )
    if (-not ('SandboxMcp.Screen' -as [type])) {
        Add-Type -Namespace SandboxMcp -Name Screen -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)] public struct COORD { public short X; public short Y; }

[StructLayout(LayoutKind.Sequential)] public struct SMALL_RECT {
    public short Left; public short Top; public short Right; public short Bottom;
}

[StructLayout(LayoutKind.Sequential)] public struct CONSOLE_SCREEN_BUFFER_INFO {
    public COORD dwSize;
    public COORD dwCursorPosition;
    public short wAttributes;
    public SMALL_RECT srWindow;
    public COORD dwMaximumWindowSize;
}

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool AttachConsole(uint dwProcessId);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool FreeConsole();

[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr sec,
                                        uint creation, uint flags, IntPtr template);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetConsoleScreenBufferInfo(IntPtr h, out CONSOLE_SCREEN_BUFFER_INFO info);

[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern bool ReadConsoleOutputCharacterW(IntPtr h, System.Text.StringBuilder buf,
                                                      uint len, COORD coord, out uint read);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool CloseHandle(IntPtr h);
'@
    }

    $rows = @()
    [void][SandboxMcp.Screen]::FreeConsole()
    if (-not [SandboxMcp.Screen]::AttachConsole([uint32]$ConsolePid)) {
        throw "AttachConsole failed for pid $ConsolePid (error $([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
    }
    try {
        # GENERIC_READ|GENERIC_WRITE as an explicit uint32: PowerShell reads 0xC0000000 as a
        # negative Int32 and the marshaller rejects it.
        $access = [uint32]3221225472
        $handle = [SandboxMcp.Screen]::CreateFileW('CONOUT$', $access, [uint32]3, [IntPtr]::Zero, [uint32]3, [uint32]0, [IntPtr]::Zero)
        if ($handle.ToInt64() -eq -1 -or $handle -eq [IntPtr]::Zero) { throw 'could not open CONOUT$' }
        try {
            $info = New-Object SandboxMcp.Screen+CONSOLE_SCREEN_BUFFER_INFO
            if (-not [SandboxMcp.Screen]::GetConsoleScreenBufferInfo($handle, [ref]$info)) {
                throw 'GetConsoleScreenBufferInfo failed'
            }
            $width = [int]$info.dwSize.X
            # Up to the cursor rather than the viewport: that captures output which has already
            # scrolled past, which is usually the part worth reading.
            $lastRow = [int]$info.dwCursorPosition.Y
            $firstRow = 0
            if ($Lines -gt 0 -and ($lastRow - $Lines + 1) -gt 0) { $firstRow = $lastRow - $Lines + 1 }
            for ($y = $firstRow; $y -le $lastRow; $y++) {
                $buffer = New-Object System.Text.StringBuilder ($width + 1)
                $coord = New-Object SandboxMcp.Screen+COORD
                $coord.X = 0
                $coord.Y = [int16]$y
                $read = [uint32]0
                if ([SandboxMcp.Screen]::ReadConsoleOutputCharacterW($handle, $buffer, [uint32]$width, $coord, [ref]$read)) {
                    # Only $read characters are valid; the rest of the builder is uninitialised.
                    $rows += $buffer.ToString(0, [int]$read).TrimEnd()
                }
            }
        } finally {
            [void][SandboxMcp.Screen]::CloseHandle($handle)
        }
    } finally {
        [void][SandboxMcp.Screen]::FreeConsole()
    }

    while ($rows.Count -gt 0 -and $rows[-1] -eq '') { $rows = $rows[0..($rows.Count - 2)] }
    if ($Lines -gt 0 -and $rows.Count -gt $Lines) { $rows = $rows[-$Lines..-1] }
    Set-Content -LiteralPath $OutFile -Value $rows -Encoding UTF8
    $rows
}

function global:Set-ConsoleForVideo {
    param(
        [int]$FontHeight = 22,
        [double]$WidthFraction = 0.80,
        [double]$HeightFraction = 0.74,
        [int]$ReserveBottom = -1
    )
    if (-not ('SandboxMcp.Console' -as [type])) {
        Add-Type -Namespace SandboxMcp -Name Console -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct COORD { public short X; public short Y; }

[StructLayout(LayoutKind.Sequential)]
public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct CONSOLE_FONT_INFO_EX {
    public uint cbSize;
    public uint nFont;
    public COORD dwFontSize;
    public int FontFamily;
    public int FontWeight;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string FaceName;
}

[DllImport("kernel32.dll", SetLastError = true)]
public static extern IntPtr GetStdHandle(int nStdHandle);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool SetCurrentConsoleFontEx(IntPtr hOut, bool maximumWindow, ref CONSOLE_FONT_INFO_EX info);

[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetCurrentConsoleFontEx(IntPtr hOut, bool maximumWindow, ref CONSOLE_FONT_INFO_EX info);

[DllImport("kernel32.dll")]
public static extern IntPtr GetConsoleWindow();

[DllImport("user32.dll")]
public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

[DllImport("user32.dll")]
public static extern bool GetWindowRect(IntPtr hWnd, ref RECT rect);

[DllImport("user32.dll")]
public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);

[DllImport("user32.dll")]
public static extern bool SystemParametersInfoW(uint uiAction, uint uiParam, ref RECT pvParam, uint fWinIni);
'@
    }

    $stdout = [SandboxMcp.Console]::GetStdHandle(-11)
    $hwnd = [SandboxMcp.Console]::GetConsoleWindow()

    try {
        $info = New-Object SandboxMcp.Console+CONSOLE_FONT_INFO_EX
        $info.cbSize = [uint32][System.Runtime.InteropServices.Marshal]::SizeOf($info)
        $info.FaceName = 'Consolas'
        $info.FontFamily = 54
        $info.FontWeight = 400
        $size = New-Object SandboxMcp.Console+COORD
        $size.X = 0
        $size.Y = [int16]$FontHeight
        $info.dwFontSize = $size
        [void][SandboxMcp.Console]::SetCurrentConsoleFontEx($stdout, $false, [ref]$info)
    } catch {
        Write-Host "[warn] console font not applied: $_"
    }

    # Rows and columns are in cells, so the window size depends on the font that was just applied.
    $cell = New-Object SandboxMcp.Console+CONSOLE_FONT_INFO_EX
    $cell.cbSize = [uint32][System.Runtime.InteropServices.Marshal]::SizeOf($cell)
    [void][SandboxMcp.Console]::GetCurrentConsoleFontEx($stdout, $false, [ref]$cell)
    $cellW = [int]$cell.dwFontSize.X
    $cellH = [int]$cell.dwFontSize.Y
    if ($cellW -le 0) { $cellW = [int][Math]::Max(1, $FontHeight / 2) }
    if ($cellH -le 0) { $cellH = $FontHeight }

    # SPI_GETWORKAREA excludes the taskbar, which is what the old maximise call ran underneath.
    $work = New-Object SandboxMcp.Console+RECT
    if (-not [SandboxMcp.Console]::SystemParametersInfoW(0x0030, 0, [ref]$work, 0)) {
        $work.Left = 0; $work.Top = 0; $work.Right = 1600; $work.Bottom = 860
    }
    if ($ReserveBottom -lt 0) { $ReserveBottom = Get-OverlayBarHeight }
    $work.Bottom = [Math]::Max($work.Top + 200, $work.Bottom - $ReserveBottom)
    $workW = $work.Right - $work.Left
    $workH = $work.Bottom - $work.Top

    $cols = [int][Math]::Floor(($workW * $WidthFraction) / $cellW)
    $rows = [int][Math]::Floor(($workH * $HeightFraction) / $cellH)
    $cols = [Math]::Max(80, [Math]::Min(200, $cols))
    $rows = [Math]::Max(15, [Math]::Min(60, $rows))

    try {
        [void][SandboxMcp.Console]::ShowWindow($hwnd, 9)
        $raw = $Host.UI.RawUI
        # Shrink the viewport first when narrowing, or the buffer cannot follow it.
        $shrunk = New-Object System.Management.Automation.Host.Size(
            [Math]::Min($cols, $raw.WindowSize.Width), [Math]::Min($rows, $raw.WindowSize.Height))
        $raw.WindowSize = $shrunk
        $raw.BufferSize = New-Object System.Management.Automation.Host.Size($cols, 9000)
        $raw.WindowSize = New-Object System.Management.Automation.Host.Size($cols, $rows)
    } catch {
        Write-Host "[warn] console size not applied: $_"
    }

    $rect = New-Object SandboxMcp.Console+RECT
    [void][SandboxMcp.Console]::GetWindowRect($hwnd, [ref]$rect)
    $winW = $rect.Right - $rect.Left
    $winH = $rect.Bottom - $rect.Top
    $x = $work.Left + [int](($workW - $winW) / 2)
    $y = $work.Top + [int](($workH - $winH) / 2)
    if ($x -lt $work.Left) { $x = $work.Left }
    if ($y -lt $work.Top) { $y = $work.Top }
    # SWP_NOSIZE | SWP_NOZORDER
    [void][SandboxMcp.Console]::SetWindowPos($hwnd, [IntPtr]::Zero, $x, $y, 0, 0, 0x0001 -bor 0x0004)

    "console ${cols}x${rows} cells, window ${winW}x${winH} at ${x},${y}, work area ${workW}x${workH} (reserved ${ReserveBottom}px)"
}
