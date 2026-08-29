# The on-camera agenda: a full-screen card shown once at the start, and a timeline bar docked at
# the bottom of the guest desktop for the rest of the run.
#
# It lives in the guest because it has to be inside the recording. It is deliberately not a window:
# no title bar, no menu, no taskbar button, no border - chrome would say "an app is showing you a
# progress bar" when the point is that the desktop itself is narrating.
#
# It never takes focus (WS_EX_NOACTIVATE) and never eats a click (HTTRANSPARENT), because the same
# session is being driven by synthesised keystrokes aimed at the console.
#
# State arrives as two files the host replaces whole: agenda.json (the chapter list, written once)
# and overlay.json (where we are now). Reading is polling rather than signalling so a torn or
# briefly-locked read just retries on the next tick.

param([int]$PollMs = 90)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'guest.ps1')

Initialize-Ctl
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$source = @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.Windows.Forms;

namespace SandboxMcp
{
    public class Bead
    {
        public string Title = "";
        public int Number = 0;
        public int State = 0;      // 0 pending, 1 active, 2 done
        public double Fill = 0.0;  // 0..1, already smoothed by the caller
        public List<Bead> Tasks = new List<Bead>();
    }

    public static class Ink
    {
        public static readonly Color Back = Color.FromArgb(0x0B, 0x0D, 0x10);
        public static readonly Color Deep = Color.FromArgb(0x11, 0x17, 0x1F);
        public static readonly Color Hair = Color.FromArgb(0x1E, 0x24, 0x2C);
        public static readonly Color Track = Color.FromArgb(0x20, 0x26, 0x2F);
        public static readonly Color Done = Color.FromArgb(0x49, 0x52, 0x5F);
        public static readonly Color Live = Color.FromArgb(0x33, 0xC0, 0x6E);
        public static readonly Color Wait = Color.FromArgb(0xD0, 0x8A, 0x3A);
        public static readonly Color Fail = Color.FromArgb(0xC8, 0x40, 0x2F);
        public static readonly Color Text = Color.FromArgb(0xF2, 0xF4, 0xF8);
        public static readonly Color Dim = Color.FromArgb(0x8B, 0x96, 0xA5);
        public static readonly Color Faint = Color.FromArgb(0x5B, 0x65, 0x73);

        public static Color Fade(Color color, double alpha)
        {
            int a = (int)Math.Round(255 * Math.Max(0.0, Math.Min(1.0, alpha)));
            return Color.FromArgb(a, color.R, color.G, color.B);
        }

        public static GraphicsPath Rounded(Rectangle r, int radius)
        {
            GraphicsPath path = new GraphicsPath();
            int d = Math.Max(1, radius * 2);
            if (r.Width < d || r.Height < d)
            {
                path.AddRectangle(r);
                return path;
            }
            path.AddArc(r.X, r.Y, d, d, 180, 90);
            path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
            path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
            path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
            path.CloseFigure();
            return path;
        }
    }

    // A surface, not a window: it cannot be focused, cannot be clicked, and is not in alt-tab.
    public class QuietForm : Form
    {
        protected override bool ShowWithoutActivation { get { return true; } }

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                cp.ExStyle |= 0x08000000; // WS_EX_NOACTIVATE - keystrokes must never land here
                cp.ExStyle |= 0x00000080; // WS_EX_TOOLWINDOW - keep it out of alt-tab
                return cp;
            }
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == 0x0084) { m.Result = (IntPtr)(-1); return; } // WM_NCHITTEST -> HTTRANSPARENT
            base.WndProc(ref m);
        }

        protected override void OnPaintBackground(PaintEventArgs e) { }

        public QuietForm()
        {
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            BackColor = Ink.Back;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.UserPaint
                     | ControlStyles.OptimizedDoubleBuffer, true);
        }

        protected static readonly TextFormatFlags Line =
            TextFormatFlags.NoPrefix | TextFormatFlags.EndEllipsis | TextFormatFlags.VerticalCenter;
    }

    public class TimelineBar : QuietForm
    {
        public List<Bead> Chapters = new List<Bead>();
        public string Status = "running";
        public string Note = "";

        private readonly Font _live = new Font("Segoe UI", 11f, FontStyle.Bold);
        private readonly Font _rest = new Font("Segoe UI", 10f, FontStyle.Regular);
        private readonly Font _task = new Font("Segoe UI", 9f, FontStyle.Regular);

        private Color Accent()
        {
            if (Status == "failed") return Ink.Fail;
            if (Status == "waiting" || Status == "paused") return Ink.Wait;
            return Ink.Live;
        }

        private static void Bar(Graphics g, Rectangle r, double fill, Color color)
        {
            int radius = r.Height / 2;
            using (GraphicsPath track = Ink.Rounded(r, radius))
            using (SolidBrush brush = new SolidBrush(Ink.Track))
                g.FillPath(brush, track);

            double clamped = Math.Max(0.0, Math.Min(1.0, fill));
            if (clamped <= 0.002) return;
            int width = (int)Math.Round(r.Width * clamped);
            if (width < r.Height) width = r.Height; // a sliver still has to read as a rounded cap
            using (GraphicsPath path = Ink.Rounded(new Rectangle(r.X, r.Y, width, r.Height), radius))
            using (SolidBrush brush = new SolidBrush(color))
                g.FillPath(brush, path);
        }

        // Widths: the chapter in flight takes most of the bar so its tasks are legible, and the
        // rest compress to the right. Everything stays on screen - that is the whole point.
        private int[] Share(int available, int active)
        {
            int n = Chapters.Count;
            int[] widths = new int[n];
            if (n == 1) { widths[0] = available; return widths; }

            int even = available / n;
            if (active < 0)
            {
                for (int i = 0; i < n; i++) widths[i] = even;
                return widths;
            }

            int wide = (int)(available * 0.44);
            int other = (available - wide) / (n - 1);
            if (other < 92) { other = Math.Min(even, 92); wide = available - other * (n - 1); }
            if (wide < other * 3 / 2) { wide = even; other = even; }

            for (int i = 0; i < n; i++) widths[i] = (i == active) ? wide : other;
            return widths;
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            g.Clear(Ink.Back);
            using (Pen hair = new Pen(Ink.Hair))
                g.DrawLine(hair, 0, 0, Width, 0);
            if (Chapters.Count == 0) return;

            const int pad = 26, gap = 14;
            int n = Chapters.Count;
            int available = Width - pad * 2 - gap * (n - 1);
            if (available < n * 40) return;

            int active = -1;
            for (int i = 0; i < n; i++) if (Chapters[i].State == 1) active = i;
            int[] widths = Share(available, active);
            Color accent = Accent();

            int x = pad;
            for (int i = 0; i < n; i++)
            {
                Bead chapter = Chapters[i];
                bool isActive = i == active;
                int w = widths[i];

                string label = chapter.State == 2
                    ? "\u2713  " + chapter.Title
                    : chapter.Number + ".  " + chapter.Title;
                Color ink = chapter.State == 2 ? Ink.Faint : (isActive ? Ink.Text : Ink.Dim);
                TextRenderer.DrawText(g, label, isActive ? _live : _rest,
                                      new Rectangle(x, 10, w, 20), ink, Line);

                Color fill = chapter.State == 2 ? Ink.Done : accent;
                Bar(g, new Rectangle(x, 38, w, 8), chapter.Fill, fill);

                if (isActive) DrawDetail(g, chapter, x, w, accent);
                x += w + gap;
            }
        }

        // The active chapter broken out underneath: its tasks, sized to the space it just took.
        private void DrawDetail(Graphics g, Bead chapter, int x, int w, Color accent)
        {
            if (Note.Length > 0 && Status != "running")
            {
                TextRenderer.DrawText(g, Note, _task, new Rectangle(x, 56, w, 18),
                                      Status == "failed" ? Ink.Fail : Ink.Wait, Line);
                return;
            }
            if (chapter.Tasks.Count == 0) return;

            int count = chapter.Tasks.Count;
            int gap = 12;
            int each = (w - gap * (count - 1)) / count;
            if (each < 54) return; // too cramped to read; the chapter bar alone carries it

            int tx = x;
            for (int i = 0; i < count; i++)
            {
                Bead task = chapter.Tasks[i];
                Color ink = task.State == 2 ? Ink.Faint : (task.State == 1 ? Ink.Dim : Ink.Faint);
                TextRenderer.DrawText(g, task.Title, _task, new Rectangle(tx, 56, each, 16), ink, Line);
                Bar(g, new Rectangle(tx, 76, each, 5), task.Fill,
                    task.State == 2 ? Ink.Done : accent);
                tx += each + gap;
            }
        }
    }

    // Shown once, before anything happens, so the run has a stated shape. It is a card rather than
    // a slide: no chrome, same palette as the bar it hands over to.
    public class AgendaCard : QuietForm
    {
        public List<Bead> Chapters = new List<Bead>();
        public string Title = "";
        public string Subtitle = "";
        public bool ShowTasks = true;
        public double Reveal = 0.0; // 0..1, staggers the rows in

        private readonly Font _title = new Font("Segoe UI Light", 34f, FontStyle.Regular);
        private readonly Font _sub = new Font("Segoe UI", 14f, FontStyle.Regular);
        private readonly Font _row = new Font("Segoe UI", 17f, FontStyle.Regular);
        private readonly Font _task = new Font("Segoe UI", 11f, FontStyle.Regular);
        private readonly Font _badge = new Font("Segoe UI", 11f, FontStyle.Bold);

        protected override void OnPaint(PaintEventArgs e)
        {
            Graphics g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = TextRenderingHint.ClearTypeGridFit;
            using (LinearGradientBrush bg =
                       new LinearGradientBrush(ClientRectangle, Ink.Deep, Ink.Back, 90f))
                g.FillRectangle(bg, ClientRectangle);

            int left = (int)(Width * 0.11);
            int width = Width - left * 2;
            double head = Math.Min(1.0, Reveal * 3.0);

            // The card has to be readable at a glance, so it gives up detail before it gives up
            // fitting: tasks go first, then row height.
            int n = Math.Max(1, Chapters.Count);
            int header = 66 + (Subtitle.Length > 0 ? 32 : 0) + 40;
            int margin = (int)(Height * 0.09);
            bool tasks = ShowTasks;
            int row = tasks ? 70 : 52;
            if (row * n > Height - header - margin * 2) { tasks = false; row = 52; }
            if (row * n > Height - header - margin * 2)
                row = Math.Max(32, (Height - header - margin * 2) / n);

            int y = Math.Max(margin, (Height - header - row * n) / 2);
            TextRenderer.DrawText(g, Title, _title, new Rectangle(left, y, width, 62),
                                  Ink.Fade(Ink.Text, head), Line);
            y += 66;
            if (Subtitle.Length > 0)
            {
                TextRenderer.DrawText(g, Subtitle, _sub, new Rectangle(left, y, width, 28),
                                      Ink.Fade(Ink.Dim, head), Line);
                y += 32;
            }
            using (Pen rule = new Pen(Ink.Fade(Ink.Live, head * 0.8), 2f))
                g.DrawLine(rule, left, y + 14, left + 96, y + 14);
            y += 40;

            for (int i = 0; i < Chapters.Count; i++)
            {
                Bead chapter = Chapters[i];
                double alpha = Math.Max(0.0, Math.Min(1.0, Reveal * (n + 2) - (i + 1)));
                if (alpha <= 0.01) continue;
                int slide = (int)((1.0 - alpha) * 14);
                int top = y + i * row + slide;
                bool detail = tasks && chapter.Tasks.Count > 0;
                int titleTop = detail ? top + 8 : top + (row - 32) / 2;

                Rectangle badge = new Rectangle(left, titleTop, 32, 32);
                using (Pen ring = new Pen(Ink.Fade(Ink.Live, alpha * 0.55), 1.6f))
                    g.DrawEllipse(ring, badge);
                TextRenderer.DrawText(g, chapter.Number.ToString(), _badge, badge,
                                      Ink.Fade(Ink.Live, alpha),
                                      TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter);

                int textLeft = left + 52;
                int textWidth = width - 52;
                TextRenderer.DrawText(g, chapter.Title, _row,
                                      new Rectangle(textLeft, titleTop, textWidth, 32),
                                      Ink.Fade(Ink.Text, alpha), Line);

                if (detail)
                {
                    string[] names = new string[chapter.Tasks.Count];
                    for (int t = 0; t < chapter.Tasks.Count; t++) names[t] = chapter.Tasks[t].Title;
                    TextRenderer.DrawText(g, string.Join("   \u00b7   ", names), _task,
                                          new Rectangle(textLeft, titleTop + 32, textWidth, 22),
                                          Ink.Fade(Ink.Dim, alpha * 0.9), Line);
                }
            }
        }
    }
}
'@

Add-Type -TypeDefinition $source -ReferencedAssemblies 'System.Windows.Forms', 'System.Drawing'

$barHeight = Get-OverlayBarHeight
$area = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
if ($env:SANDBOX_OVERLAY_BOUNDS -match '^\s*(\d+),(\d+),(\d+),(\d+)\s*$') {
    # Overridable so the layout can be checked at the sandbox's window size from a host desktop
    # that is a different shape, the same way SANDBOX_CTL lets these scripts run off-sandbox.
    $area = New-Object System.Drawing.Rectangle(
        [int]$Matches[1], [int]$Matches[2], [int]$Matches[3], [int]$Matches[4])
}

$bar = New-Object SandboxMcp.TimelineBar
$bar.Bounds = New-Object System.Drawing.Rectangle(
    $area.Left, ($area.Bottom - $barHeight), $area.Width, $barHeight)

$card = New-Object SandboxMcp.AgendaCard
$card.Bounds = $area
$card.Opacity = 0

$global:OvAgendaPath = Get-CtlPath 'agenda.json'
$global:OvStatePath = Get-CtlPath 'overlay.json'
$global:OvAgendaStamp = [DateTime]::MinValue
$global:OvStateStamp = [DateTime]::MinValue
$global:OvState = $null
$global:OvChanged = [DateTime]::UtcNow
$global:OvPosition = @{ chapter = 0; task = 0 }
$global:OvCardShown = $null
$global:OvBar = $bar
$global:OvCard = $card

function Read-OverlayJson {
    # The host replaces these files whole, so a failed read is a race worth retrying, not an error.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][ref]$Stamp)
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        if ($item.LastWriteTimeUtc -le $Stamp.Value) { return $null }
        $text = [System.IO.File]::ReadAllText($Path)
        $value = $text | ConvertFrom-Json
        $Stamp.Value = $item.LastWriteTimeUtc
        return $value
    } catch {
        return $null
    }
}

function New-Bead {
    param([Parameter(Mandatory)]$Chapter)
    $bead = New-Object SandboxMcp.Bead
    $bead.Number = [int]$Chapter.index
    $bead.Title = [string]$Chapter.title
    foreach ($task in @($Chapter.tasks)) {
        $item = New-Object SandboxMcp.Bead
        $item.Title = [string]$task
        $bead.Tasks.Add($item)
    }
    $bead
}

function Set-Agenda {
    param([Parameter(Mandatory)]$Agenda)
    $global:OvCard.Title = [string]$Agenda.title
    $global:OvCard.Subtitle = [string]$Agenda.subtitle
    $global:OvCard.ShowTasks = [bool]$Agenda.show_tasks
    $global:OvCard.Chapters.Clear()
    $global:OvBar.Chapters.Clear()
    # Two independent bead trees: the card and the bar animate at different rates.
    foreach ($chapter in @($Agenda.chapters)) {
        $global:OvCard.Chapters.Add((New-Bead -Chapter $chapter))
        $global:OvBar.Chapters.Add((New-Bead -Chapter $chapter))
    }
    if (-not $global:OvBar.Visible) { $global:OvBar.Show() }
}

function Get-Creep {
    # A bar has to move while a ten-minute install runs, but it must not promise a finish it cannot
    # know about: ease asymptotically towards the next real boundary and let the next step snap it.
    param([double]$Floor, [double]$Ceiling, [double]$Elapsed)
    if ($Ceiling -le $Floor) { return $Floor }
    $eased = 1 - [Math]::Exp(-$Elapsed / 12.0)
    $Floor + ($Ceiling - $Floor) * [Math]::Min(0.93, $eased)
}

function Set-BeadState {
    param([Parameter(Mandatory)]$Bead, [int]$Position, [int]$Active, [double]$Target, [bool]$AllDone)
    if ($AllDone -or $Position -lt $Active) {
        $Bead.State = 2
        $goal = 1.0
    } elseif ($Position -eq $Active) {
        $Bead.State = 1
        $goal = $Target
    } else {
        $Bead.State = 0
        $goal = 0.0
    }
    # Tween rather than jump, so a completed step reads as the bar catching up.
    $Bead.Fill = $Bead.Fill + ($goal - $Bead.Fill) * 0.16
    if ([Math]::Abs($goal - $Bead.Fill) -lt 0.004) { $Bead.Fill = $goal }
}

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [Math]::Max(40, $PollMs)
$timer.Add_Tick({
    try {
        $agenda = Read-OverlayJson -Path $global:OvAgendaPath -Stamp ([ref]$global:OvAgendaStamp)
        if ($agenda) { Set-Agenda -Agenda $agenda }

        $state = Read-OverlayJson -Path $global:OvStatePath -Stamp ([ref]$global:OvStateStamp)
        if ($state) {
            $moved = ($global:OvPosition.chapter -ne [int]$state.chapter) -or
                     ($global:OvPosition.task -ne [int]$state.task)
            if ($moved) { $global:OvChanged = [DateTime]::UtcNow }
            $global:OvPosition = @{ chapter = [int]$state.chapter; task = [int]$state.task }
            $global:OvState = $state
        }

        $bar = $global:OvBar
        if ($bar.Chapters.Count -eq 0) { return }
        $state = $global:OvState

        $status = if ($state -and $state.status) { [string]$state.status } else { 'running' }
        $mode = if ($state -and $state.mode) { [string]$state.mode } else { 'timeline' }
        $bar.Status = $status
        $bar.Note = if ($state) { [string]$state.note } else { '' }

        $allDone = $status -eq 'done'
        $elapsed = ([DateTime]::UtcNow - $global:OvChanged).TotalSeconds
        $chapterAt = $global:OvPosition.chapter
        $taskAt = $global:OvPosition.task
        $chapterFill = 0.0
        $taskFill = 0.0
        if ($state) {
            $chapterFill = Get-Creep -Floor ([double]$state.chapter_progress) `
                -Ceiling ([double]$state.chapter_next) -Elapsed $elapsed
            $taskFill = Get-Creep -Floor ([double]$state.task_progress) `
                -Ceiling ([double]$state.task_next) -Elapsed $elapsed
        }

        for ($i = 0; $i -lt $bar.Chapters.Count; $i++) {
            $chapter = $bar.Chapters[$i]
            Set-BeadState -Bead $chapter -Position ($i + 1) -Active $chapterAt `
                -Target $chapterFill -AllDone $allDone
            for ($t = 0; $t -lt $chapter.Tasks.Count; $t++) {
                $active = if ($chapter.State -eq 1) { $taskAt } elseif ($chapter.State -eq 2) { $chapter.Tasks.Count + 1 } else { 0 }
                Set-BeadState -Bead $chapter.Tasks[$t] -Position ($t + 1) -Active $active `
                    -Target $taskFill -AllDone ($allDone -or $chapter.State -eq 2)
            }
        }
        $bar.Invalidate()

        $card = $global:OvCard
        if ($mode -eq 'agenda') {
            if (-not $global:OvCardShown) {
                $global:OvCardShown = [DateTime]::UtcNow
                $card.Reveal = 0
                $card.Opacity = 0
                $card.Show()
            }
            $shown = ([DateTime]::UtcNow - $global:OvCardShown).TotalSeconds
            $card.Opacity = [Math]::Min(1.0, $shown / 0.4)
            if ($card.Reveal -lt 1) {
                $card.Reveal = [Math]::Min(1.0, $shown / 1.6)
                $card.Invalidate()
            }
        } elseif ($global:OvCardShown) {
            $card.Opacity = $card.Opacity - 0.09
            if ($card.Opacity -le 0.02) {
                $card.Hide()
                $global:OvCardShown = $null
            }
        }

        if ($mode -eq 'hidden' -and $bar.Visible) { $bar.Hide() }
        elseif ($mode -ne 'hidden' -and -not $bar.Visible) { $bar.Show() }
    } catch {
        # A cosmetic overlay must never take the guest session with it.
        try { Write-GuestLog -Message "overlay tick failed: $_" -Name 'overlay' } catch { }
    }
})
$timer.Start()

mark 'overlay_ready' -Detail "bar ${barHeight}px at $($area.Width)x$($area.Height)"
[System.Windows.Forms.Application]::Run((New-Object System.Windows.Forms.ApplicationContext))
