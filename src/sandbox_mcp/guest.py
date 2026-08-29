"""Host end of the guest channel: status, markers, and the inbox/outbox command pump.

Everything travels through the read-write folder mapped to C:\\ctl. Each file has exactly one
writer and is published by rename, so the host never reads a half-written document.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUS_FILE = "status.json"
BOOT_FILE = "boot.json"
AGENT_FILE = "agent.json"
SCREEN_FILE = "screen.txt"
TRANSCRIPT_FILE = "transcript.log"
AGENDA_FILE = "agenda.json"
OVERLAY_FILE = "overlay.json"
MARKERS_DIR = "markers"
INBOX_DIR = "inbox"
OUTBOX_DIR = "outbox"

GUEST_LIB = r". C:\payload\lib\guest.ps1"
_SAFE_MARKER = re.compile(r"[^A-Za-z0-9._-]")


class GuestError(RuntimeError):
    pass


@dataclass(frozen=True)
class GuestChannel:
    control_dir: Path

    def _read_json(self, path: Path, attempts: int = 5) -> dict[str, Any] | None:
        for attempt in range(attempts):
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                return None
            except (OSError, json.JSONDecodeError):
                time.sleep(0.05 * (attempt + 1))
        return None

    def status(self) -> dict[str, Any]:
        return self._read_json(self.control_dir / STATUS_FILE) or {}

    def boot(self) -> dict[str, Any]:
        return self._read_json(self.control_dir / BOOT_FILE) or {}

    def agent(self) -> dict[str, Any]:
        return self._read_json(self.control_dir / AGENT_FILE) or {}

    def markers(self) -> list[dict[str, Any]]:
        directory = self.control_dir / MARKERS_DIR
        if not directory.is_dir():
            return []
        found = [self._read_json(f) for f in sorted(directory.glob("*.json"))]
        return sorted((m for m in found if m), key=lambda m: m.get("ts", ""))

    def marker(self, name: str) -> dict[str, Any] | None:
        safe = _SAFE_MARKER.sub("_", name)
        return self._read_json(self.control_dir / MARKERS_DIR / f"{safe}.json")

    def transcript_tail(self, lines: int = 80) -> list[str]:
        path = self.control_dir / TRANSCRIPT_FILE
        if not path.is_file():
            return []
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        return text.splitlines()[-max(1, lines) :]

    def last_typed_command(self) -> str | None:
        """Start-Transcript echoes each typed line with a PS> prefix, which is the only record of
        what the guest actually received."""
        for line in reversed(self.transcript_tail(500)):
            if line.startswith("PS>"):
                return line[3:].strip()
        return None

    def screen(self) -> list[str]:
        path = self.control_dir / SCREEN_FILE
        if not path.is_file():
            return []
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    def wait_for_marker(self, name: str, timeout: float, poll: float = 0.4) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.marker(name)
            if found:
                return found
            time.sleep(poll)
        tail = "\n".join(self.transcript_tail(12))
        raise GuestError(
            f"The guest never published marker {name!r} within {timeout:.0f}s. "
            f"Markers so far: {[m.get('name') for m in self.markers()] or 'none'}."
            + (f"\nGuest transcript tail:\n{tail}" if tail else "")
        )

    def wait_for_phase(self, phase: str, timeout: float, poll: float = 0.5) -> dict[str, Any]:
        # Once the interactive prompt takes over the phase becomes "prompt", which still means ready.
        accepted = {phase, "prompt"} if phase == "ready" else {phase}
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.status()
            if last.get("phase") in accepted:
                return last
            time.sleep(poll)
        raise GuestError(
            f"The guest never reported phase {phase!r} within {timeout:.0f}s (last status: {last or 'none'}). "
            "Check that the payload folder is mapped and bootstrap.ps1 ran."
        )

    def submit(self, command: str) -> str:
        """Drop a job for the hidden agent. The .ps1 extension is applied by rename so the pump
        never sees a partial script."""
        job_id = f"job-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
        inbox = self.control_dir / INBOX_DIR
        inbox.mkdir(parents=True, exist_ok=True)
        staging = inbox / f"{job_id}.staging"
        staging.write_text(f"{GUEST_LIB}\n{command}\n", encoding="utf-8")
        staging.rename(inbox / f"{job_id}.ps1")
        return job_id

    def _publish(self, name: str, value: dict[str, Any]) -> dict[str, Any]:
        """Host-to-guest files, replaced whole so the overlay never paints a half-written frame.

        The guest polls these, and a replace loses to a reader that has the file open for the
        instant it takes to read it, so retry rather than fail a run over a cosmetic write.
        """
        target = self.control_dir / name
        staging = target.with_suffix(".staging")
        staging.write_text(json.dumps(value), encoding="utf-8")
        for attempt in range(4):
            try:
                os.replace(staging, target)
                return value
            except OSError:
                time.sleep(0.05 * (attempt + 1))
        raise GuestError(f"Could not publish {name} to the guest control folder")

    def publish_agenda(self, agenda: dict[str, Any]) -> dict[str, Any]:
        """The chapter list the on-camera overlay draws its card and timeline bar from."""
        return self._publish(AGENDA_FILE, agenda)

    def publish_overlay(self, **state: Any) -> dict[str, Any]:
        """Move the timeline bar. Merged onto the last position so a caller can change one field.

        Safe to read-modify-write: the host is the only writer of this file.
        """
        current = self._read_json(self.control_dir / OVERLAY_FILE) or {}
        current.update({k: v for k, v in state.items() if v is not None})
        current["ts"] = time.time()
        return self._publish(OVERLAY_FILE, current)

    def wait_for_result(self, job_id: str, timeout: float, poll: float = 0.4) -> dict[str, Any]:
        target = self.control_dir / OUTBOX_DIR / f"{job_id}.json"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._read_json(target)
            if result:
                return result
            time.sleep(poll)
        raise GuestError(
            f"Job {job_id} did not finish within {timeout:.0f}s. "
            f"Agent status: {self.agent() or 'no heartbeat - is bootstrap.ps1 running?'}"
        )

    def run_hidden(self, command: str, timeout: float) -> dict[str, Any]:
        return self.wait_for_result(self.submit(command), timeout)

    def wait_for_prompt(self, after_index: int, timeout: float, poll: float = 0.3) -> dict[str, Any]:
        """The guest console's prompt function bumps prompt_index once a command has finished."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.status()
            if int(status.get("prompt_index") or 0) > after_index:
                return status
            time.sleep(poll)
        raise GuestError(
            f"The visible command did not return to a prompt within {timeout:.0f}s. "
            "Use run_in_guest(visible=false) for anything long-running, or raise the timeout."
        )
