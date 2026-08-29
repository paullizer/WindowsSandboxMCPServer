"""Generates .wsb configs, launches Windows Sandbox, and owns the session lifecycle."""

from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import windows, wininput
from .config import (
    SANDBOX_PROCESSES,
    SANDBOX_VM_PROCESSES,
    SANDBOX_WINDOW_PROCESSES,
    Settings,
    find_windows_sandbox,
)
from .guest import GuestChannel, GuestError

GUEST_PAYLOAD_DIR = r"C:\payload"
GUEST_CONTROL_DIR = r"C:\ctl"
BOOTSTRAP_NAME = "bootstrap.ps1"
LIVE_OVERLAY_SENTINEL = "live-overlay"
# -NoExit leaves the console at an interactive prompt, which is the terminal seen on camera.
LOGON_COMMAND = (
    "powershell -ExecutionPolicy Bypass -NoProfile -NoExit "
    f"-File {GUEST_PAYLOAD_DIR}\\{BOOTSTRAP_NAME}"
)
# The RDP view inside the sandbox window; keystrokes only reach the guest when it has focus.
INPUT_CAPTURE_CLASS = "IHWindowClass"
MIN_SESSION_WIDTH, MIN_SESSION_HEIGHT = 640, 400
# The guest agent heartbeats every few seconds; older than this and its sandbox is gone.
STALE_HEARTBEAT_SECONDS = 60.0
MIN_MEMORY_MB = 2048


class SandboxError(RuntimeError):
    pass


@dataclass
class SandboxSession:
    session_id: str
    config_path: Path
    control_dir: Path
    payload_dir: Path | None
    memory_mb: int
    networking: bool
    vgpu: bool
    requested_client_size: tuple[int, int]
    started_at: float
    hwnd: int = 0
    pid: int = 0
    phase: str = "launching"
    events: list[str] = field(default_factory=list)

    def uptime(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


def build_wsb(
    *,
    memory_mb: int,
    networking: bool,
    vgpu: bool,
    clipboard: bool,
    mapped_folders: list[tuple[Path, str, bool]],
    logon_command: str | None,
) -> str:
    """Render a .wsb document. Built as XML rather than text so paths cannot inject markup."""
    root = ET.Element("Configuration")
    ET.SubElement(root, "MemoryInMB").text = str(max(MIN_MEMORY_MB, int(memory_mb)))
    ET.SubElement(root, "vGPU").text = "Enable" if vgpu else "Disable"
    ET.SubElement(root, "Networking").text = "Enable" if networking else "Disable"
    ET.SubElement(root, "ClipboardRedirection").text = "Enable" if clipboard else "Disable"
    if mapped_folders:
        folders = ET.SubElement(root, "MappedFolders")
        for host_folder, guest_folder, read_only in mapped_folders:
            node = ET.SubElement(folders, "MappedFolder")
            ET.SubElement(node, "HostFolder").text = str(host_folder)
            ET.SubElement(node, "SandboxFolder").text = guest_folder
            ET.SubElement(node, "ReadOnly").text = "true" if read_only else "false"
    if logon_command:
        node = ET.SubElement(root, "LogonCommand")
        ET.SubElement(node, "Command").text = logon_command
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


class SandboxDriver:
    """One sandbox at a time - Windows Sandbox does not support concurrent instances."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session: SandboxSession | None = None

    @property
    def session(self) -> SandboxSession | None:
        return self._session

    def running_instances(self) -> list[dict[str, Any]]:
        return windows.find_processes(*SANDBOX_WINDOW_PROCESSES)

    def launch(
        self,
        *,
        payload_dir: str | None = None,
        networking: bool | None = None,
        memory_mb: int | None = None,
        vgpu: bool | None = None,
        width: int | None = None,
        height: int | None = None,
        clipboard: bool = True,
        timeout: float | None = None,
    ) -> SandboxSession:
        settings = self._settings
        if self._session is not None and self.running_instances():
            raise SandboxError(
                "A sandbox session is already active. Call teardown_sandbox before launching another."
            )
        strays = self.running_instances()
        if strays:
            pids = ", ".join(str(p["pid"]) for p in strays)
            raise SandboxError(
                f"Windows Sandbox is already running (pid {pids}) and only one instance is "
                "supported. Close it, or call teardown_sandbox(force=true)."
            )

        install = find_windows_sandbox()
        if install is None:
            raise SandboxError(
                "WindowsSandbox.exe was not found. Enable the 'Windows Sandbox' optional feature "
                "and reboot; see check_sandbox_setup."
            )

        resolved_payload = self._resolve_payload_dir(payload_dir)
        session_id = f"sbx-{datetime.now():%Y%m%d-%H%M%S}"
        session_dir = settings.session_dir(session_id)
        control_dir = session_dir / "control"
        control_dir.mkdir(parents=True, exist_ok=True)
        # bootstrap.ps1 only starts the overlay when this exists, so a take that composites its
        # agenda in post never runs a drawing process in the guest at all.
        if settings.live_overlay:
            (control_dir / LIVE_OVERLAY_SENTINEL).write_text("1", encoding="utf-8")

        mapped: list[tuple[Path, str, bool]] = [(control_dir, GUEST_CONTROL_DIR, False)]
        logon_command = None
        if resolved_payload is not None:
            mapped.insert(0, (resolved_payload, GUEST_PAYLOAD_DIR, True))
            if (resolved_payload / BOOTSTRAP_NAME).is_file():
                logon_command = LOGON_COMMAND

        config_path = session_dir / "sandbox.wsb"
        config_path.write_text(
            build_wsb(
                memory_mb=memory_mb if memory_mb is not None else settings.memory_mb,
                networking=settings.networking if networking is None else networking,
                vgpu=settings.vgpu if vgpu is None else vgpu,
                clipboard=clipboard,
                mapped_folders=mapped,
                logon_command=logon_command,
            ),
            encoding="utf-8",
        )

        client_size = (
            width if width is not None else settings.window_width,
            height if height is not None else settings.window_height,
        )
        session = SandboxSession(
            session_id=session_id,
            config_path=config_path,
            control_dir=control_dir,
            payload_dir=resolved_payload,
            memory_mb=max(MIN_MEMORY_MB, memory_mb if memory_mb is not None else settings.memory_mb),
            networking=settings.networking if networking is None else networking,
            vgpu=settings.vgpu if vgpu is None else vgpu,
            requested_client_size=client_size,
            started_at=time.monotonic(),
        )
        self._session = session

        launcher = subprocess.Popen([install["launcher"], str(config_path)])
        session.events.append("launched")

        deadline = timeout if timeout is not None else settings.boot_timeout_seconds
        try:
            info = self._await_window(launcher, deadline)
        except SandboxError:
            self._session = None
            raise

        session.hwnd = info["handle"]
        session.pid = info["pid"]
        session.events.append(f"window_found:{info['class_name']}")

        actual = windows.set_window_client_size(session.hwnd, *client_size)
        session.events.append(f"client_size:{actual['width']}x{actual['height']}")
        session.phase = "window_ready"
        return session

    def focus(self) -> dict[str, Any]:
        session = self._require_session()
        if not windows.force_foreground(session.hwnd):
            raise SandboxError(
                "The sandbox window could not be brought to the foreground. Input is refused "
                "rather than risking keystrokes landing on the host - clear any modal dialog or "
                "full-screen app and retry."
            )
        # The client resizes itself once the guest session connects, so re-pin it here.
        resized = None
        current = windows.client_size(session.hwnd)
        wanted = {"width": session.requested_client_size[0], "height": session.requested_client_size[1]}
        if current != wanted:
            resized = windows.set_window_client_size(session.hwnd, wanted["width"], wanted["height"])
            time.sleep(0.4)
        input_focus = self._ensure_input_focus(session)
        return {
            "window": windows.window_info(session.hwnd),
            "input_focus": input_focus,
            "resized_to": resized,
        }

    def reattach(self, session_id: str | None = None) -> SandboxSession:
        """Adopt a sandbox this driver did not launch, or lost when the server restarted.

        The guest survives a host restart untouched; only the in-process handle is lost. Rebuilding
        instead would throw away whatever the guest has already installed or signed in to.
        """
        if self._session is not None and self._session.hwnd and windows.user32.IsWindow(self._session.hwnd):
            raise SandboxError(
                f"Already attached to {self._session.session_id}. Call teardown_sandbox first."
            )

        found = [w for name in SANDBOX_WINDOW_PROCESSES for w in windows.windows_for_process(name)]
        sessions = [
            w for w in found
            if w["client_size"]["width"] >= MIN_SESSION_WIDTH
            and w["client_size"]["height"] >= MIN_SESSION_HEIGHT
        ]
        if not sessions:
            raise SandboxError(
                "No running sandbox session window was found. "
                + ("Windows Sandbox is running but has not opened a session window yet."
                   if found else "Call launch_sandbox to start one.")
            )
        sessions.sort(key=lambda w: w["client_size"]["width"] * w["client_size"]["height"], reverse=True)
        window = sessions[0]

        root = self._settings.work_dir / "sessions"
        # The agent rewrites its heartbeat continuously, so file age is the liveness signal. The
        # "alive" flag inside it is not - a dead session leaves it frozen at true forever.
        def heartbeat_age(directory: Path) -> float:
            return time.time() - (directory / "control" / "agent.json").stat().st_mtime

        if session_id:
            session_dir = root / session_id
            if not (session_dir / "control" / "agent.json").is_file():
                raise SandboxError(f"No guest control folder for session {session_id!r} under {root}.")
        else:
            candidates = [d for d in root.glob("sbx-*") if (d / "control" / "agent.json").is_file()]
            if not candidates:
                raise SandboxError(
                    f"No session control folder with a guest agent was found under {root}. "
                    "The sandbox may have been launched without the payload mapped."
                )
            session_dir = min(candidates, key=heartbeat_age)

        control_dir = session_dir / "control"
        age = heartbeat_age(session_dir)
        if age > STALE_HEARTBEAT_SECONDS:
            raise SandboxError(
                f"The guest agent in {session_dir.name} last checked in {age:.0f}s ago, so that "
                "control folder belongs to a sandbox that is already gone. The running sandbox was "
                "launched by something else, or with a different SANDBOX_MCP_WORK_DIR than this "
                f"server uses ({self._settings.work_dir}). Tear it down and launch a fresh one."
            )

        settings = self._settings
        session = SandboxSession(
            session_id=session_dir.name,
            config_path=session_dir / "sandbox.wsb",
            control_dir=control_dir,
            payload_dir=settings.payload_dir,
            memory_mb=settings.memory_mb,
            networking=settings.networking,
            vgpu=settings.vgpu,
            requested_client_size=(window["client_size"]["width"], window["client_size"]["height"]),
            started_at=time.monotonic(),
            hwnd=window["handle"],
            pid=window["pid"],
            phase="window_ready",
        )
        session.events.append(f"reattached:{window['class_name']}")
        self._session = session
        return session

    def info(self) -> dict[str, Any]:
        session = self._session
        if session is None:
            return {
                "active": False,
                "running_instances": self.running_instances(),
                "work_dir": str(self._settings.work_dir),
            }
        alive = bool(session.hwnd) and bool(windows.user32.IsWindow(session.hwnd))
        window = windows.window_info(session.hwnd) if alive else None
        return {
            "active": alive,
            "session_id": session.session_id,
            "phase": session.phase if alive else "gone",
            "uptime_seconds": round(session.uptime(), 1),
            "window": window,
            "foreground": windows.is_foreground(session.hwnd) if alive else False,
            "requested_client_size": {
                "width": session.requested_client_size[0],
                "height": session.requested_client_size[1],
            },
            # The guest reports its own resolution over the control channel in phase 3; until then
            # the client area is the only thing the host can measure, so scale is unknown.
            "guest_resolution": None,
            "scale": None,
            "config_path": str(session.config_path),
            "control_dir": str(session.control_dir),
            "payload_dir": str(session.payload_dir) if session.payload_dir else None,
            "memory_mb": session.memory_mb,
            "networking": session.networking,
            "vgpu": session.vgpu,
            "events": session.events,
        }

    def teardown(self, *, force: bool = False, grace_seconds: float = 3.0) -> dict[str, Any]:
        session = self._session
        if session is None and not force:
            raise SandboxError(
                "No sandbox session is active. Pass force=true to close a sandbox this server "
                "did not launch."
            )

        if session is not None and session.hwnd and windows.user32.IsWindow(session.hwnd):
            windows.close_window(session.hwnd)
            windows.wait_for_processes_gone(SANDBOX_PROCESSES, timeout=grace_seconds)

        # Closing prompts for confirmation inside the sandbox, so terminate what is left.
        terminated = [
            process["pid"]
            for process in windows.find_processes(*SANDBOX_PROCESSES)
            if windows.terminate_process(process["pid"])
        ]
        gone = windows.wait_for_processes_gone(SANDBOX_PROCESSES, timeout=20.0)
        # A new sandbox cannot start until the VM itself is reclaimed.
        vm_gone = windows.wait_for_processes_gone(SANDBOX_VM_PROCESSES, timeout=60.0)

        result: dict[str, Any] = {
            "torn_down": gone,
            "vm_released": vm_gone,
            "terminated_pids": terminated,
            "session_id": session.session_id if session else None,
            "control_dir": str(session.control_dir) if session else None,
        }
        if session is not None:
            session.config_path.unlink(missing_ok=True)
            result["config_removed"] = True
        self._session = None
        if not gone:
            raise SandboxError(
                "Windows Sandbox processes are still running after teardown; check Task Manager."
            )
        return result

    def _require_session(self) -> SandboxSession:
        session = self._session
        if session is None or not session.hwnd or not windows.user32.IsWindow(session.hwnd):
            raise SandboxError("No sandbox window is available. Call launch_sandbox first.")
        return session

    def require_foreground(self, auto_focus: bool = True) -> SandboxSession:
        """Every input batch goes through here; unverified focus means input hits the host."""
        session = self._require_session()
        if auto_focus and not windows.is_foreground(session.hwnd):
            windows.force_foreground(session.hwnd)
        if not windows.is_foreground(session.hwnd):
            raise SandboxError(
                "The sandbox window is not the foreground window, so input is refused - "
                "injecting it now would send keystrokes to the host. Clear whatever stole "
                "focus and call focus_sandbox."
            )
        self._ensure_input_focus(session)
        return session

    def _ensure_input_focus(self, session: SandboxSession) -> str:
        """Foreground is not enough: the WinUI shell swallows keys unless the RDP input-capture
        child window holds the keyboard focus."""
        child = windows.find_descendant(session.hwnd, INPUT_CAPTURE_CLASS)
        if child is None:
            return "capture_window_not_found"
        if windows.focused_child(session.hwnd) == child:
            return "already_focused"
        if windows.set_child_focus(session.hwnd, child):
            return "set_focus"
        origin = windows.client_origin(session.hwnd)
        size = windows.client_size(session.hwnd)
        wininput.click_at(
            origin["x"] + size["width"] // 2,
            origin["y"] + size["height"] // 2,
            move_duration_ms=120,
        )
        time.sleep(0.2)
        if windows.focused_child(session.hwnd) != child:
            raise SandboxError(
                "The sandbox session view would not take keyboard focus, so input is refused - "
                "it would be swallowed by the sandbox window chrome."
            )
        return "clicked"

    def guest_to_screen(self, session: SandboxSession, x: int, y: int) -> tuple[int, int]:
        origin = windows.client_origin(session.hwnd)
        size = windows.client_size(session.hwnd)
        if not (0 <= x < size["width"] and 0 <= y < size["height"]):
            raise SandboxError(
                f"({x}, {y}) is outside the {size['width']}x{size['height']} guest area"
            )
        return origin["x"] + int(x), origin["y"] + int(y)

    def send_keys(self, chord: str) -> dict[str, Any]:
        self.require_foreground()
        try:
            return dict(wininput.send_chord(chord))
        except wininput.InputError as exc:
            raise SandboxError(str(exc)) from exc

    def type_text(self, text: str, cps: float = 14.0, allow_secret: bool = False) -> dict[str, Any]:
        self._guard_secret(text, allow_secret, "type_text")
        self.require_foreground()
        try:
            typed = wininput.type_text(text, cps)
        except wininput.InputError as exc:
            raise SandboxError(str(exc)) from exc
        return {"characters": typed, "cps": cps}

    def paste_text(
        self, text: str, restore_clipboard: bool = True, allow_secret: bool = False
    ) -> dict[str, Any]:
        self._guard_secret(text, allow_secret, "paste_text")
        self.require_foreground()
        try:
            return dict(wininput.paste_text(text, restore=restore_clipboard))
        except wininput.InputError as exc:
            raise SandboxError(str(exc)) from exc

    def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        double: bool = False,
        move_duration_ms: int = 250,
    ) -> dict[str, Any]:
        session = self.require_foreground()
        screen_x, screen_y = self.guest_to_screen(session, x, y)
        try:
            wininput.click_at(screen_x, screen_y, button, double, move_duration_ms)
        except wininput.InputError as exc:
            raise SandboxError(str(exc)) from exc
        return {"guest": {"x": x, "y": y}, "screen": {"x": screen_x, "y": screen_y},
                "button": button, "double": double}

    def move_mouse(self, x: int, y: int, duration_ms: int = 250) -> dict[str, Any]:
        session = self.require_foreground()
        screen_x, screen_y = self.guest_to_screen(session, x, y)
        wininput.move_mouse_to(screen_x, screen_y, duration_ms)
        return {"guest": {"x": x, "y": y}, "screen": {"x": screen_x, "y": screen_y}}

    def scroll(self, notches: int) -> dict[str, Any]:
        self.require_foreground()
        wininput.scroll_wheel(notches)
        return {"notches": int(notches)}

    def channel(self) -> GuestChannel:
        return GuestChannel(self._require_session().control_dir)

    def publish_agenda(self, agenda: dict[str, Any]) -> dict[str, Any]:
        """Hand the chapter list to the in-guest overlay, when there is one."""
        if not self._settings.live_overlay:
            return {"drawn": False, "reason": "live overlay is off; the agenda is composited in post"}
        self.channel().publish_agenda(agenda)
        return {"drawn": True, "chapters": len(agenda.get("chapters") or ())}

    def set_overlay(self, **state: Any) -> dict[str, Any]:
        if not self._settings.live_overlay:
            return {"drawn": False}
        self.channel().publish_overlay(**state)
        return {"drawn": True}

    def show_agenda(self, seconds: float = 12.0) -> dict[str, Any]:
        """Hold the shot on the agenda card.

        The beat is held whether or not anything is drawn: with the live overlay off this is the
        slot the post-production card is composited into, and a step is never trimmed from the
        inside, so the room survives the edit.
        """
        held = max(0.0, min(float(seconds), self._settings.max_wait_seconds))
        drawn = self.set_overlay(status="agenda")["drawn"]
        time.sleep(held)
        if drawn:
            self.set_overlay(status="running")
        return {"seconds": round(held, 2), "drawn": drawn}

    def run_hidden(self, command: str, timeout: float) -> dict[str, Any]:
        return self.channel().run_hidden(command, timeout)

    def read_guest_screen(self, lines: int = 0) -> dict[str, Any]:
        """What the guest console is showing right now, including output of a command still
        running - which the transcript cannot give us until it finishes."""
        channel = self.channel()
        console_pid = channel.status().get("console_pid")
        if not console_pid:
            raise SandboxError("The guest has not reported its console yet; call await_desktop first.")
        result = channel.run_hidden(
            f"Get-GuestScreen -ConsolePid {int(console_pid)} -Lines {int(lines)} | Out-Null", 90
        )
        if result.get("exit_code") != 0:
            raise SandboxError(
                "Could not read the guest console screen: "
                f"{result.get('stderr_tail') or result.get('stdout_tail') or 'no output'}"
            )
        return {"lines": channel.screen()}

    def answer_guest(
        self,
        expect: str,
        send: str = "",
        keys: tuple[str, ...] = (),
        submit: bool = True,
        timeout: float = 300.0,
        lines: int = 25,
        poll: float = 2.0,
        paste: bool = False,
        active_rows: int = 8,
        cps: float = 14.0,
    ) -> dict[str, Any]:
        """Wait for an interactive prompt to appear on the guest console, then answer it.

        Reading the screen is what makes this safe: an installer or CLI that stops to ask something
        is invisible to the transcript, so answering blind means typing into the wrong question.

        The answer is typed, not pasted. CLIs that take over the terminal to prompt - azd, and most
        pickers - read keystrokes directly and never see a ctrl+v, so a pasted answer silently
        arrives empty. Set paste=True only for a prompt long enough that mistyping is the bigger
        risk.
        """
        pattern = re.compile(expect, re.IGNORECASE)
        deadline = time.monotonic() + timeout
        screen: list[str] = []
        matched = ""
        while time.monotonic() < deadline:
            screen = self.read_guest_screen(lines)["lines"]
            # Only the bottom of the buffer is a live prompt; matching further up answers a
            # question that has already scrolled past.
            hit = next(
                (line for line in reversed(screen[-max(1, active_rows):]) if pattern.search(line)),
                None,
            )
            if hit:
                matched = hit
                break
            time.sleep(poll)
        if not matched:
            raise SandboxError(
                f"The guest never showed a prompt matching {expect!r} within {timeout:.0f}s. "
                f"Last screen:\n" + "\n".join(screen[-12:])
            )

        time.sleep(0.6)
        if send:
            if paste:
                self.paste_text(send)
            else:
                self.type_text(send, cps=cps)
            time.sleep(0.4)
        for chord in keys:
            self.send_keys(chord)
            time.sleep(0.4)
        if submit and not keys:
            self.send_keys("enter")
        return {"matched": matched.strip(), "sent": send, "keys": list(keys), "pasted": paste}

    def await_desktop(self, timeout: float) -> dict[str, Any]:
        session = self._require_session()
        if session.payload_dir is None:
            raise SandboxError(
                "This sandbox was launched without a payload folder, so no guest agent is "
                "running and readiness cannot be reported. Relaunch with the payload mapped."
            )
        channel = GuestChannel(session.control_dir)
        # The marker is published last, so waiting on the phase alone races the rest of bootstrap.
        channel.wait_for_marker("desktop_ready", timeout)
        channel.wait_for_marker("console_ready", 30)
        session.phase = "guest_ready"
        session.events.append("guest_ready")
        return {**channel.status(), "boot": channel.boot()}

    def focus_guest_console(self, timeout: float = 30.0) -> dict[str, Any]:
        """Only the guest can raise a guest window, so ask the agent to do it."""
        channel = self.channel()
        console_pid = channel.status().get("console_pid")
        if not console_pid:
            raise SandboxError("The guest has not reported its console yet; call await_desktop first.")
        result = channel.run_hidden(
            f"if (-not (Show-GuestConsole -ConsolePid {int(console_pid)})) {{ exit 1 }}", timeout
        )
        if result.get("exit_code") != 0:
            raise SandboxError(
                "The guest console would not come to the foreground, so a visible command would "
                "be typed into the wrong window. "
                f"Guest said: {result.get('stdout_tail') or ''} {result.get('stderr_tail') or ''}".strip()
            )
        return result

    def run_visible(self, command: str, await_exit: bool, timeout: float) -> dict[str, Any]:
        """Paste the command into the on-screen console and press Enter - this one is on camera."""
        self._guard_secret(command, False, "run_in_guest")
        channel = self.channel()
        self.focus_guest_console()
        self.require_foreground()
        time.sleep(0.3)
        before = int(channel.status().get("prompt_index") or 0)
        wininput.paste_text(command)
        time.sleep(0.4)
        wininput.send_chord("enter")
        if not await_exit:
            return {"visible": True, "command": command, "awaited": False}
        status = channel.wait_for_prompt(before, timeout)
        exit_code = status.get("last_exit_code")
        succeeded = status.get("last_succeeded")
        delivered = channel.last_typed_command()
        return {
            "visible": True,
            "command": command,
            "awaited": True,
            "exit_code": exit_code,
            # exit_code only tracks native commands; $? is what catches a failing cmdlet.
            "succeeded": succeeded,
            "ok": bool(succeeded) and exit_code in (0, None),
            "delivered": delivered,
            "input_clean": delivered is None or delivered == command.strip(),
            "transcript_tail": channel.transcript_tail(20),
        }

    @staticmethod
    def _guard_secret(text: str, allow: bool, tool: str) -> None:
        if allow:
            return
        label = wininput.detect_secret(text)
        if label:
            raise SandboxError(
                f"{tool} refused: this text looks like a {label} and the screen may be "
                "recording. Prefer `az login --use-device-code`, or pass allow_secret=true "
                "if this is a false positive."
            )

    def _resolve_payload_dir(self, payload_dir: str | None) -> Path:
        raw = payload_dir if payload_dir else None
        candidate = Path(raw).expanduser().resolve() if raw else self._settings.payload_dir
        if candidate is None:
            raise SandboxError(
                "Payload folder was not found. Reinstall windows-sandbox-mcp or set "
                "SANDBOX_MCP_PAYLOAD_DIR to the repository's payload folder."
            )
        if not candidate.is_dir():
            raise SandboxError(f"Payload folder does not exist: {candidate}")
        if candidate.parent == candidate:
            raise SandboxError("Refusing to map a drive root into the sandbox; use a narrow folder.")
        bootstrap = candidate / BOOTSTRAP_NAME
        if not bootstrap.is_file():
            raise SandboxError(f"Payload folder has no required {BOOTSTRAP_NAME}: {candidate}")
        return candidate

    def _await_window(self, launcher: subprocess.Popen[bytes], timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(5.0, timeout)
        seen_small = False
        while time.monotonic() < deadline:
            found = [w for name in SANDBOX_WINDOW_PROCESSES for w in windows.windows_for_process(name)]
            # A failed start shows a small message window; a real session is the full client area.
            sessions = [
                w for w in found
                if w["client_size"]["width"] >= MIN_SESSION_WIDTH
                and w["client_size"]["height"] >= MIN_SESSION_HEIGHT
            ]
            if sessions:
                sessions.sort(key=lambda w: w["client_size"]["width"] * w["client_size"]["height"], reverse=True)
                return sessions[0]
            seen_small = seen_small or bool(found)
            code = launcher.poll()
            if code not in (None, 0):
                raise SandboxError(f"WindowsSandbox.exe exited with code {code} before a window appeared")
            time.sleep(0.5)
        if seen_small:
            raise SandboxError(
                "Windows Sandbox opened a message window instead of a session, which means it "
                "refused to start. The usual cause is a previous instance still shutting down - "
                "wait for vmmemWindowsSandbox to disappear, then retry."
            )
        raise SandboxError(
            f"No sandbox window appeared within {timeout:.0f}s. Cold start can be slow - "
            "raise SANDBOX_MCP_BOOT_TIMEOUT_SECONDS or check that virtualization is available."
        )
