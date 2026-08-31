"""MCP server exposing Windows Sandbox lifecycle control to a coding agent."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from . import __version__, windows
from .config import (
    SANDBOX_WINDOW_PROCESSES,
    Settings,
    find_windows_sandbox,
    find_wsb_cli,
    loaded_env_file,
    virtualization_state,
    windows_edition,
)
from .guest import GuestError
from .sandbox import SandboxDriver, SandboxError
from .storyboard import StoryboardError, load as load_storyboard, run as run_storyboard_steps
from . import speech
from . import timeline
from . import transcript as transcript_writer

INSTRUCTIONS = """\
Drives a local Windows Sandbox so you can perform an install-and-deploy walkthrough on a pristine
machine, usually while the Snagit MCP server records the screen.

Lifecycle: check_sandbox_setup -> launch_sandbox -> await_desktop -> focus_sandbox -> drive the
guest -> teardown_sandbox.

Rules that matter:
- Only one Windows Sandbox instance can exist at a time. Tear the current one down before
  launching another; launch_sandbox refuses rather than fighting an existing instance.
- The sandbox window must be the foreground window before any input is sent. Every input tool
  verifies this with GetForegroundWindow and fails loudly rather than letting keystrokes land on
  the host.
- Nothing on the host may steal focus mid-take. Turn on Do Not Disturb, and do not run host
  terminal commands or use the keyboard while the sandbox is being driven.
- Synchronise with await_desktop and await_guest, never with beat. The guest knows when an
  installer finished; beat is for pacing a shot, not for waiting on work.
- run_in_guest(visible=true) pastes into the on-screen console and is a story beat.
  run_in_guest(visible=false) runs silently through the hidden agent - use it for setup, fixtures
  and anything that should not appear in the video. Both return a real exit code.
- Work keyboard-first. Win to open the start menu, ctrl+l for a browser address bar, win+r for a
  run box, ctrl+j for downloads. Reserve click for genuine GUI moments.
- Prefer paste_text over type_text. paste_text cannot mistype a long URL or azd command. Use
  type_text only for short strings you want visibly typed for narrative effect.
- click and move_mouse take guest-relative coordinates: (0, 0) is the top left of the guest
  desktop, not the host screen.
- Pace every visible step with beat. Instant automation is unwatchable.
- The guest image is minimal: PowerShell, cmd, explorer and curl, but no browser, no Store, no
  winget and no Notepad. Install what a demo needs with curl or an install script.
- The sandbox is fully ephemeral - everything inside is discarded at teardown. The only thing that
  survives is the read-write control folder, which is a real write path onto the host, so keep it
  narrow and never map a broad host directory into the guest.
- Never put a secret on screen. type_text, paste_text and visible run_in_guest refuse text that
  looks like a credential. Prefer `az login --use-device-code`.
- A repeatable take belongs in a storyboard, and storyboards now live in the `storyboard` MCP
  server: it replays declarative beats across this sandbox and other actors, synthesizes the
  narration and writes the run manifest the video editor cuts from. Drive a take from there and
  use these tools for the sandbox itself. Set SANDBOX_MCP_LEGACY_STORYBOARD=1 to re-expose the
  storyboard tools here, which is only useful for rolling back.
- Long-running guest scripts should call `mark` as they progress. The editor never trims inside a
  step that published no markers, so a ten-minute wait that reported nothing survives whole.
"""

settings = Settings.from_env()
_driver: SandboxDriver | None = None
_LEGACY_STORYBOARD = os.environ.get(
    "SANDBOX_MCP_LEGACY_STORYBOARD", ""
).strip().lower() in {"1", "true", "yes", "on"}


def _cues_path() -> Path:
    return settings.ensure_work_dir() / "transcripts" / "cues.json"


def _load_cues() -> list[dict[str, Any]]:
    """Narration lives on disk, not in memory: a take spans several calls and this server may be
    restarted mid-take without the sandbox ever stopping."""
    path = _cues_path()
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return loaded if isinstance(loaded, list) else []


def _save_cues(cues: list[dict[str, Any]]) -> None:
    path = _cues_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cues, indent=2), encoding="utf-8")


def _narration_dir(storyboard_name: str) -> Path:
    safe = "".join(c for c in storyboard_name if c.isalnum() or c in "-_") or "storyboard"
    return settings.ensure_work_dir() / "narration" / safe

server = MCPServer(
    name="sandbox",
    title="Windows Sandbox Driver",
    version=__version__,
    instructions=INSTRUCTIONS,
)


def _legacy_tool() -> Callable[[Any], Any]:
    """Registers a tool only when the legacy storyboard surface is switched back on.

    The storyboard engine now lives in StoryboardMCPServer, which exposes the same seven tools
    against its own work directory. Advertising both would let one take write half its state to
    each. The code stays here until a mixed take is proven there; set
    SANDBOX_MCP_LEGACY_STORYBOARD=1 to get it back without an edit.
    """
    return server.tool() if _LEGACY_STORYBOARD else (lambda fn: fn)


def get_driver() -> SandboxDriver:
    global _driver
    if _driver is None:
        windows.ensure_dpi_awareness()
        _driver = SandboxDriver(settings)
    return _driver


async def _input(method: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    """Input blocks on inter-key delays, so it runs off the event loop."""
    try:
        return await asyncio.to_thread(method, *args)
    except SandboxError as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool()
async def check_sandbox_setup() -> dict[str, Any]:
    """Verify this host can run Windows Sandbox: edition, feature state, virtualization, tooling."""
    edition = windows_edition()
    install = find_windows_sandbox()
    virtualization = virtualization_state()
    wsb_cli = find_wsb_cli()
    running = windows.find_processes(*SANDBOX_WINDOW_PROCESSES)

    blockers: list[str] = []
    if not edition["supported"]:
        blockers.append(
            f"Windows edition {edition['edition_id']!r} cannot run Windows Sandbox "
            "(Pro, Enterprise or Education required)."
        )
    if install is None:
        blockers.append(
            "WindowsSandbox.exe not found - enable the 'Windows Sandbox' optional feature and reboot."
        )
    if virtualization["services"]["CmService"] == "absent":
        blockers.append("Container Manager Service (CmService) is absent - the feature is not installed.")
    if settings.payload_dir is None:
        blockers.append(
            "Payload folder not found - reinstall windows-sandbox-mcp or set "
            "SANDBOX_MCP_PAYLOAD_DIR."
        )
    elif not (settings.payload_dir / "bootstrap.ps1").is_file():
        blockers.append(f"Payload folder has no required bootstrap.ps1: {settings.payload_dir}")
    if not virtualization["hypervisor_running"]:
        # These processor bits are hidden once a hypervisor owns them, so only trust them here.
        if not virtualization["slat"]:
            blockers.append("The processor does not report SLAT, which Windows Sandbox requires.")
        if not virtualization["virtualization_firmware_enabled"]:
            blockers.append("Hardware virtualization is disabled in firmware; enable it in BIOS/UEFI.")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "edition": edition,
        "sandbox_feature": {
            "installed": install is not None,
            "launcher": install["launcher"] if install else None,
            "clients": install["clients"] if install else [],
        },
        "virtualization": virtualization,
        "wsb_cli": wsb_cli,
        "work_dir": str(settings.ensure_work_dir()),
        "payload_dir": str(settings.payload_dir) if settings.payload_dir else None,
        "env_file": str(loaded_env_file()) if loaded_env_file() else None,
        "dpi_awareness": windows.ensure_dpi_awareness(),
        "displays": len(windows.list_monitors()),
        "narration": speech.narration_status(settings),
        "default_client_size": {
            "width": settings.window_width,
            "height": settings.window_height,
        },
        "running_instances": running,
    }


@server.tool()
async def launch_sandbox(
    payload_dir: str = "",
    networking: bool = True,
    memory_mb: int = 8192,
    vgpu: bool = True,
    width: int = 1600,
    height: int = 900,
    clipboard: bool = True,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Generate a .wsb config, start Windows Sandbox, and pin its window to a fixed client size.

    Returns once the sandbox window exists - the guest desktop is still booting at that point.
    payload_dir is mapped read-only to C:\\payload; a private control folder is mapped read-write
    to C:\\ctl. Only one sandbox can run at a time.
    """
    driver = get_driver()
    try:
        await asyncio.to_thread(
            driver.launch,
            payload_dir=payload_dir or None,
            networking=networking,
            memory_mb=memory_mb,
            vgpu=vgpu,
            width=width,
            height=height,
            clipboard=clipboard,
            timeout=timeout,
        )
    except SandboxError as exc:
        raise RuntimeError(str(exc)) from exc
    return driver.info()


@server.tool()
async def reattach_sandbox(session_id: str = "") -> dict[str, Any]:
    """Adopt a sandbox that is already running, instead of launching a new one.

    Use this after restarting this server: the guest keeps running untouched, but the handle to it
    is lost, so tools report no active session. Reattaching recovers whatever the guest already has
    installed or signed in to. Defaults to the session whose guest agent wrote most recently.
    """
    driver = get_driver()
    try:
        await asyncio.to_thread(driver.reattach, session_id or None)
    except SandboxError as exc:
        raise RuntimeError(str(exc)) from exc
    return driver.info()


@server.tool()
async def sandbox_info() -> dict[str, Any]:
    """Report the current session: window handle, bounds, client origin and size, and phase."""
    return get_driver().info()


@server.tool()
async def focus_sandbox() -> dict[str, Any]:
    """Foreground the sandbox, give its session view the keyboard, and re-pin the window size.

    Call this before driving the guest. It raises rather than returning false, because
    unverified focus means injected keystrokes would land on the host.
    """
    driver = get_driver()
    try:
        result = await asyncio.to_thread(driver.focus)
    except SandboxError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"foreground": True, **result}


@server.tool()
async def teardown_sandbox(force: bool = False) -> dict[str, Any]:
    """Close the sandbox and discard everything inside it.

    Set force=true to also close a Windows Sandbox instance this server did not launch.
    """
    driver = get_driver()
    try:
        return await asyncio.to_thread(driver.teardown, force=force)
    except SandboxError as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool()
async def beat(seconds: float, reason: str = "") -> dict[str, Any]:
    """Hold still for a moment so the last on-screen action is readable in the video."""
    if seconds <= 0:
        raise ValueError("seconds must be greater than 0")
    capped = min(float(seconds), settings.max_wait_seconds)
    await asyncio.sleep(capped)
    return {"waited_seconds": capped, "reason": reason, "capped": capped < float(seconds)}


@server.tool()
async def send_keys(chord: str) -> dict[str, Any]:
    """Send one key chord to the guest, e.g. "win", "enter", "ctrl+l", "ctrl+shift+escape".

    Modifiers are ctrl / alt / shift / win (plus the right* variants); the final part is a key
    name such as a letter, digit, f1-f12, tab, escape, up, home or delete.
    """
    return await _input(get_driver().send_keys, chord)


@server.tool()
async def type_text(text: str, cps: float = 14.0, allow_secret: bool = False) -> dict[str, Any]:
    """Type text one character at a time, human-paced, so it reads on camera.

    Use this only for short strings you want visibly typed. Anything long or exact should go
    through paste_text, which cannot mistype. Refuses text that looks like a credential.
    """
    return await _input(get_driver().type_text, text, cps, allow_secret)


@server.tool()
async def paste_text(
    text: str, restore_clipboard: bool = True, allow_secret: bool = False
) -> dict[str, Any]:
    """Put text on the clipboard and send ctrl+v into the guest.

    Preferred over type_text for URLs, commands and anything that must be exact. The host
    clipboard is put back afterwards. Refuses text that looks like a credential.
    """
    return await _input(get_driver().paste_text, text, restore_clipboard, allow_secret)


@server.tool()
async def click(
    x: int,
    y: int,
    button: str = "left",
    double: bool = False,
    move_duration_ms: int = 250,
) -> dict[str, Any]:
    """Click at guest-relative coordinates - (0, 0) is the top left of the guest desktop.

    Reserve this for genuine GUI moments; keyboard navigation is more reliable and looks better.
    """
    return await _input(get_driver().click, x, y, button, double, move_duration_ms)


@server.tool()
async def move_mouse(x: int, y: int, duration_ms: int = 250) -> dict[str, Any]:
    """Travel the cursor to guest-relative coordinates smoothly; instant jumps look broken."""
    return await _input(get_driver().move_mouse, x, y, duration_ms)


@server.tool()
async def scroll(notches: int) -> dict[str, Any]:
    """Scroll the wheel in the guest; positive scrolls up, negative scrolls down."""
    return await _input(get_driver().scroll, notches)


@server.tool()
async def await_desktop(timeout: float = 120.0) -> dict[str, Any]:
    """Block until the guest bootstrap reports it is ready, instead of guessing with beat.

    Requires a payload folder containing bootstrap.ps1 (the default one is mapped automatically).
    """
    driver = get_driver()
    try:
        status = await asyncio.to_thread(driver.await_desktop, timeout)
    except (SandboxError, GuestError) as exc:
        raise RuntimeError(str(exc)) from exc
    return {"ready": True, "status": status}


@server.tool()
async def guest_status() -> dict[str, Any]:
    """Report the guest's phase, agent heartbeat, published markers and last exit code."""
    driver = get_driver()
    try:
        channel = driver.channel()
    except SandboxError as exc:
        raise RuntimeError(str(exc)) from exc
    return {
        "status": channel.status(),
        "boot": channel.boot(),
        "agent": channel.agent(),
        "markers": channel.markers(),
        "control_dir": str(channel.control_dir),
    }


@server.tool()
async def await_guest(marker: str, timeout: float = 300.0) -> dict[str, Any]:
    """Block until a guest script publishes a marker. Prefer this over beat for synchronisation.

    Guest scripts publish one by calling `mark <name>`; beat is for pacing only.
    """
    driver = get_driver()
    try:
        channel = driver.channel()
        return await asyncio.to_thread(channel.wait_for_marker, marker, timeout)
    except (SandboxError, GuestError) as exc:
        raise RuntimeError(str(exc)) from exc


@server.tool()
async def read_guest_log(tail: int = 80) -> dict[str, Any]:
    """Read the tail of the guest console transcript."""
    driver = get_driver()
    try:
        lines = driver.channel().transcript_tail(tail)
    except SandboxError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"lines": lines, "count": len(lines)}


@server.tool()
async def read_guest_screen(lines: int = 0) -> dict[str, Any]:
    """Read what the guest console is showing right now, as text.

    Use this when a command is still running: the transcript only flushes once a command finishes,
    so it cannot show a device code, a progress bar or an interactive prompt. This can. Pass lines
    to keep only the last N rows.
    """
    driver = get_driver()
    try:
        result = await asyncio.to_thread(driver.read_guest_screen, lines)
    except (SandboxError, GuestError) as exc:
        raise RuntimeError(str(exc)) from exc
    return {**result, "count": len(result["lines"])}


@server.tool()
async def run_in_guest(
    command: str,
    visible: bool = True,
    await_exit: bool = True,
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Run a PowerShell command in the guest and return its real exit code.

    visible=true pastes it into the on-screen console and presses Enter - that is a story beat and
    it is on camera. visible=false hands it to the hidden agent, which runs it silently and returns
    stdout/stderr tails; use that for setup and anything you do not want in the video.

    A visible run also returns input_clean: false when the guest transcript does not echo exactly
    the command that was sent, which means something else typed into the session and the take is
    spoilt.
    """
    driver = get_driver()
    try:
        if visible:
            return await _input(driver.run_visible, command, await_exit, timeout)
        channel = driver.channel()
        job_id = await asyncio.to_thread(channel.submit, command)
        if not await_exit:
            return {"visible": False, "job_id": job_id, "awaited": False}
        result = await asyncio.to_thread(channel.wait_for_result, job_id, timeout)
        return {"visible": False, "awaited": True, **result}
    except (SandboxError, GuestError) as exc:
        raise RuntimeError(str(exc)) from exc


@_legacy_tool()
async def list_storyboards() -> dict[str, Any]:
    """List the storyboards available to replay."""
    entries = []
    for path in settings.list_storyboards():
        entry: dict[str, Any] = {"name": path.stem, "path": str(path)}
        try:
            board = load_storyboard(path, settings.resolve_storyboard)
            entry["description"] = board.description.strip()
            entry["steps"] = len(board.steps)
            if board.setup_steps:
                entry["setup_steps"] = len(board.setup_steps)
            if board.required_values:
                entry["configured"] = board.configured
                entry["required_values"] = list(board.required_values)
                entry["values_source"] = str(board.values_source) if board.values_source else None
        except StoryboardError as exc:
            entry["error"] = str(exc)
        entries.append(entry)
    return {
        "directory": str(settings.storyboard_dir) if settings.storyboard_dir else None,
        "storyboards": entries,
    }


@server.tool()
async def answer_guest(
    expect: str,
    send: str = "",
    keys: list[str] | None = None,
    submit: bool = True,
    timeout: float = 300.0,
    lines: int = 25,
    paste: bool = False,
    active_rows: int = 8,
    cps: float = 14.0,
) -> dict[str, Any]:
    """Wait for an interactive prompt on the guest console, then answer it.

    expect is matched case-insensitively against the bottom of the live screen, so it works for
    prompts the transcript cannot show and will not answer a question that has scrolled past. send
    is typed, because CLIs that take over the terminal never see a ctrl+v; set paste=True only when
    mistyping is the bigger risk. keys are chords sent instead of Enter, for pickers driven by
    arrows.
    """
    driver = get_driver()
    try:
        return await asyncio.to_thread(
            driver.answer_guest,
            expect,
            send,
            tuple(keys or ()),
            submit,
            timeout,
            lines,
            paste=paste,
            active_rows=active_rows,
            cps=cps,
        )
    except (SandboxError, GuestError) as exc:
        raise RuntimeError(str(exc)) from exc


@_legacy_tool()
async def run_storyboard(
    name: str,
    dry_run: bool = False,
    from_step: int = 1,
    to_step: int = 0,
    phase: str = "steps",
) -> dict[str, Any]:
    """Replay a storyboard against the running sandbox, one beat at a time.

    Every step gets an implicit trailing beat so nothing snaps instantly. Use dry_run=true to
    validate and see the resolved steps without touching the sandbox.

    A storyboard with a `setup:` list needs phase="setup" run first, before the camera rolls: that
    is where installs and other preparation the take assumes live. It has no agenda, timeline or
    narration. Then record, then call again with the default phase="steps".

    Replay stops at the first failure and reports which step broke. An await_operator step
    suspends instead: do whatever it asks (sign in, approve something), then call again with the
    from_step it hands back in resume_with. Set to_step to stop early on purpose - that is how you
    run a storyboard in segments, for instance to pause a recording between them.
    """
    try:
        path = settings.resolve_storyboard(name)
        board = load_storyboard(path, settings.resolve_storyboard)
    except (FileNotFoundError, StoryboardError) as exc:
        raise RuntimeError(str(exc)) from exc

    if dry_run:
        return {
            "storyboard": board.name,
            "path": str(path),
            "description": board.description.strip(),
            "beat_between": board.beat_between,
            "valid": True,
            "configured": board.configured,
            "required_values": list(board.required_values),
            "values_source": str(board.values_source) if board.values_source else None,
            "setup": board.outline("setup"),
            "steps": board.outline(),
        }

    try:
        board.require_configured()
    except StoryboardError as exc:
        raise RuntimeError(str(exc)) from exc

    segment_started = time.time()
    durations = speech.load_durations(_narration_dir(board.name) / speech.NARRATION_FILE)
    try:
        report = await asyncio.to_thread(
            run_storyboard_steps,
            board,
            get_driver(),
            max(1, from_step),
            max(0, to_step),
            phase,
            durations,
        )
    except StoryboardError as exc:
        raise RuntimeError(str(exc)) from exc
    # Setup happens before the camera rolls, so it contributes nothing to the transcript.
    if phase == "setup":
        return report
    try:
        session = get_driver().info()
        seen = {
            "session_id": session.get("session_id"),
            "control_dir": session.get("control_dir"),
        }
    except SandboxError:
        seen = {}
    timeline.record_steps(
        settings,
        board.name,
        board.agenda(),
        seen or None,
        report.get("steps", []),
        reset=from_step <= 1,
    )
    cues = [] if from_step <= 1 else _load_cues()
    # Stamped against the wall clock, not chained onto the previous segment: a take is usually run
    # in pieces, and the gaps between them are real time in the recording.
    for cue in report.get("transcript", []):
        cues.append({
            **cue,
            "wall_start": segment_started + float(cue["start"]),
            "wall_end": segment_started + float(cue["end"]),
        })
    _save_cues(cues)
    return report


@_legacy_tool()
async def save_transcript(
    file_name: str = "",
    offset_seconds: float = 0.0,
    directory: str = "",
) -> dict[str, Any]:
    """Write the narration collected during storyboard replay as a timed transcript.

    Cues are timed from the wall clock of the run that just happened, so they stay in sync with the
    recording even when a take is run in segments with long gaps between them. Writes .vtt
    (subtitles), .srt and a readable .md voiceover script.

    Timing is relative to the first cue. Pass offset_seconds to shift everything later if the
    recording started before the replay did.

    Segments accumulate on disk, so they survive a restart of this server: replaying from step 1
    starts a new transcript, resuming appends to it.
    """
    cues = _load_cues()
    if not cues:
        raise RuntimeError(
            "No narration was collected. Add `say:` / `say_after:` to storyboard steps, then run "
            "the storyboard before saving a transcript."
        )
    anchor = cues[0]["wall_start"]
    timed = [
        {**c, "start": c["wall_start"] - anchor, "end": c["wall_end"] - anchor} for c in cues
    ]
    target_dir = Path(directory).expanduser() if directory else settings.ensure_work_dir() / "transcripts"
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = "".join(c for c in (file_name or timed[0].get("storyboard", "transcript")) if c.isalnum() or c in "-_")
    stem = stem or "transcript"

    written = {}
    for suffix, render in (
        (".vtt", transcript_writer.to_vtt),
        (".srt", transcript_writer.to_srt),
        (".md", lambda c, o: transcript_writer.to_script(c, stem, o)),
    ):
        path = target_dir / f"{stem}{suffix}"
        path.write_text(render(timed, offset_seconds), encoding="utf-8")
        written[suffix.lstrip(".")] = str(path)

    return {
        "cues": len(timed),
        "files": written,
        "offset_seconds": offset_seconds,
        "span_seconds": round(timed[-1]["end"], 1),
    }


@_legacy_tool()
async def list_voices(locale: str = "en-US", gender: str = "", refresh: bool = False) -> dict[str, Any]:
    """Browse the Azure Speech voice gallery, so a storyboard can pick a narrator deliberately.

    Results are cached on disk; pass refresh=true to re-fetch. `supports_audio_duration` is the
    field that matters for post-production: HD voices (anything with a `:basemodel` suffix) ignore
    mstts:audioduration and so cannot be refitted to an edited slot.
    """
    cache = settings.ensure_speech_cache_dir() / f"voices-{locale or 'all'}.json"
    if not refresh and cache.is_file():
        voices = json.loads(cache.read_text(encoding="utf-8"))
    else:
        try:
            voices = await asyncio.to_thread(lambda: speech.Synthesizer(settings).voices(locale))
        except speech.SpeechError as exc:
            raise RuntimeError(str(exc)) from exc
        cache.write_text(json.dumps(voices, indent=2), encoding="utf-8")

    if gender:
        wanted = gender.strip().lower()
        voices = [v for v in voices if v["gender"].lower() == wanted]
    return {
        "locale": locale or "all",
        "count": len(voices),
        "configured_voice": settings.speech_voice,
        "voices": voices,
    }


@_legacy_tool()
async def synthesize_narration(
    name: str,
    voice: str = "",
    style: str = "",
    rate: str = "",
    phase: str = "steps",
    target_durations: dict[str, float] | None = None,
    out_dir: str = "",
) -> dict[str, Any]:
    """Speak a storyboard's `say:` / `say_after:` lines and record how long each one really takes.

    Run this BEFORE recording. The measured durations feed straight back into run_storyboard, so
    subtitles stop being a words-per-second guess, and the editor learns how much room each line
    needs before it decides what dead air to cut.

    Nothing is played during a take - the WAVs are for post-production. Pass target_durations
    (cue id -> seconds) for a refit pass once an edit is known; that needs a standard neural voice,
    because HD voices ignore mstts:audioduration.
    """
    try:
        path = settings.resolve_storyboard(name)
        board = load_storyboard(path, settings.resolve_storyboard)
        cues = speech.cues_from_storyboard(board, phase)
    except (FileNotFoundError, StoryboardError, speech.SpeechError) as exc:
        raise RuntimeError(str(exc)) from exc
    if not cues:
        raise RuntimeError(
            f"{board.name} has no narration in phase {phase!r}. Add `say:` / `say_after:` to its "
            "steps first."
        )

    target = Path(out_dir).expanduser() if out_dir else _narration_dir(board.name)
    try:
        result = await asyncio.to_thread(
            speech.synthesize_cues,
            settings,
            cues,
            target,
            voice,
            style,
            rate,
            target_durations,
        )
    except speech.SpeechError as exc:
        raise RuntimeError(str(exc)) from exc
    return {"storyboard": board.name, "phase": phase, **result}


@_legacy_tool()
async def note_recording(
    path: str = "",
    anchor: float = 0.0,
    stopped: float = 0.0,
    frame_rate: float = 0.0,
    variable_frame_rate: bool = True,
) -> dict[str, Any]:
    """Remember when the camera started, so the edit has a clock to align to.

    Call this immediately after start_recording with the file the camera is writing; anchor
    defaults to now, which is correct because start_recording only returns once the countdown has
    finished and the recording is genuinely rolling. Call it again after stop_recording with
    stopped= to record the end precisely.

    Without an anchor there is no way to map a wall-clock timestamp onto a frame, and
    export_timeline refuses to guess.
    """
    recording = timeline.note_recording(
        settings, path, anchor, stopped, frame_rate, variable_frame_rate
    )
    return {"recording": recording, "take": str(timeline.take_path(settings))}


@_legacy_tool()
async def export_timeline(
    recording: str = "",
    anchor: float = 0.0,
    stopped: float = 0.0,
    frame_rate: float = 0.0,
    file_name: str = "",
    directory: str = "",
) -> dict[str, Any]:
    """Write run.json: what the take actually did, in the shape the video editor reads.

    Steps, narration cues and every marker the guest published, all stamped against the wall clock
    so they survive a take recorded in segments with long gaps between them. This is the handoff to
    LocalVideoEditMCPServer, which derives the whole edit from it and never guesses from pixels.

    Anything not represented here cannot be trimmed. In particular the editor treats a step with no
    markers as content and keeps it whole however long it ran, so long-running guest scripts should
    call `mark` as they progress.
    """
    take = timeline.load_take(settings)
    override: dict[str, Any] = {}
    if recording:
        override["source"] = recording
    if anchor:
        override["anchor"] = float(anchor)
    if stopped:
        override["stopped"] = float(stopped)
    if frame_rate:
        override["frame_rate"] = float(frame_rate)

    try:
        run = timeline.build_run(settings, take, _load_cues(), override or None)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    target_dir = Path(directory).expanduser() if directory else settings.ensure_work_dir() / "takes"
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = "".join(c for c in (file_name or run["session_id"]) if c.isalnum() or c in "-_")
    path = target_dir / f"{stem or 'run'}.json"
    path.write_text(json.dumps(run, indent=2), encoding="utf-8")

    unmarked = [
        s["index"]
        for s in run["steps"]
        if (s["end"] - s["start"]) >= 60
        and not any(m.get("step") == s["index"] for m in run["markers"])
    ]
    return {
        "path": str(path),
        "schema": run["schema"],
        "session_id": run["session_id"],
        "storyboard": run["storyboard"],
        "steps": len(run["steps"]),
        "cues": len(run["cues"]),
        "markers": len(run["markers"]),
        "duration_seconds": run["recording"]["duration_seconds"],
        # The editor cannot trim inside these, so they will play out in full.
        "long_steps_without_markers": unmarked,
    }
