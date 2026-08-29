"""Records what a take actually did, in the shape the video editor reads.

The storyboard says what should happen; this says what did. It is the only bridge between a run and
the edit: `LocalVideoEditMCPServer` derives every cut from these timestamps and never guesses from
pixels, so anything missing here is footage that cannot be trimmed.

Markers matter more than they look. The editor treats a step with no markers as content and never
cuts it, however long it ran - so a ten-minute deploy that published no progress markers survives
whole. Long guest scripts should call `mark` as they go.

Schema is sandbox-run/1. Times are wall clock (epoch seconds); the editor subtracts
`recording.anchor` to get source-relative seconds.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import Settings

RUN_SCHEMA = "sandbox-run/1"
TAKE_FILE = "take.json"
RUN_FILE = "run.json"

# The editor refuses steps that overlap, and floating point noise should not trip that.
EPS = 0.001


def take_path(settings: Settings) -> Path:
    return settings.ensure_work_dir() / "transcripts" / TAKE_FILE


def load_take(settings: Settings) -> dict[str, Any]:
    """Take state lives on disk because a take spans several calls and may outlive this process."""
    path = take_path(settings)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_take(settings: Settings, take: dict[str, Any]) -> Path:
    path = take_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(take, indent=2), encoding="utf-8")
    return path


def note_recording(
    settings: Settings,
    path: str = "",
    anchor: float = 0.0,
    stopped: float = 0.0,
    frame_rate: float = 0.0,
    variable_frame_rate: bool = True,
) -> dict[str, Any]:
    """Remember when the camera started, which is the only clock the editor can align to."""
    take = load_take(settings)
    recording = dict(take.get("recording") or {})
    if path:
        recording["source"] = path
    if anchor:
        recording["anchor"] = round(float(anchor), 3)
    elif "anchor" not in recording:
        recording["anchor"] = round(time.time(), 3)
    if stopped:
        recording["stopped"] = round(float(stopped), 3)
    if frame_rate:
        recording["frame_rate"] = float(frame_rate)
    recording["variable_frame_rate"] = bool(variable_frame_rate)
    take["recording"] = recording
    save_take(settings, take)
    return recording


def record_steps(
    settings: Settings,
    storyboard_name: str,
    agenda: dict[str, Any] | None,
    session: dict[str, Any] | None,
    steps: list[dict[str, Any]],
    reset: bool,
) -> None:
    """Append one replay segment's steps. A take is often run in pieces, and they all count."""
    take = {} if reset else load_take(settings)
    if reset:
        # The camera note survives a restart from step 1: it describes the recording, not the run.
        previous = load_take(settings)
        if previous.get("recording"):
            take["recording"] = previous["recording"]
    take["storyboard"] = storyboard_name
    if agenda:
        take["agenda"] = agenda
    if session:
        take["session"] = session
    kept = [s for s in (take.get("steps") or []) if isinstance(s, dict)]
    kept.extend(steps)
    take["steps"] = kept
    save_take(settings, take)


def marker_time(raw: Any, fallback: float) -> float:
    """Guest markers carry a PowerShell round-trip timestamp; fall back to the file's own mtime."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw:
        text = raw.strip()
        # PowerShell's 'o' format has 7 fractional digits; fromisoformat accepts at most 6.
        head, dot, tail = text.partition(".")
        if dot:
            cut = 0
            while cut < len(tail) and tail[cut].isdigit():
                cut += 1
            text = f"{head}.{tail[:cut][:6]}{tail[cut:]}"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        return parsed.timestamp()
    return fallback


def harvest_markers(control_dir: Path | None, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every marker the guest published, timed and attributed to the step it landed in."""
    if control_dir is None:
        return []
    directory = Path(control_dir) / "markers"
    if not directory.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(document, dict):
            continue
        at = marker_time(document.get("ts"), path.stat().st_mtime)
        entry: dict[str, Any] = {"name": str(document.get("name") or path.stem), "at": round(at, 3)}
        owner = _step_at(steps, at)
        if owner is not None:
            entry["step"] = owner
        if document.get("detail"):
            entry["detail"] = str(document["detail"])
        found.append(entry)
    return sorted(found, key=lambda entry: entry["at"])


def _step_at(steps: list[dict[str, Any]], at: float) -> Any:
    for step in steps:
        if float(step.get("start", 0.0)) - EPS <= at <= float(step.get("end", 0.0)) + EPS:
            return step.get("index")
    return None


def build_run(
    settings: Settings,
    take: dict[str, Any],
    cues: Iterable[dict[str, Any]],
    recording_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble run.json. Raises ValueError when the result would be unusable to the editor."""
    steps = _clean_steps(take.get("steps") or [])
    if not steps:
        raise ValueError(
            "No steps recorded. Run the storyboard (phase='steps') before exporting a timeline."
        )

    recording = {**(take.get("recording") or {}), **(recording_override or {})}
    anchor = recording.get("anchor")
    if not isinstance(anchor, (int, float)):
        raise ValueError(
            "No recording anchor. Call note_recording right after start_recording, or pass "
            "anchor= explicitly - without it there is no clock to align the edit to."
        )
    anchor = float(anchor)

    listed = [_clean_cue(c) for c in cues]
    listed = [c for c in listed if c is not None]
    last = max(
        [float(s["end"]) for s in steps] + [float(c["wall_end"]) for c in listed] or [anchor]
    )
    if not recording.get("stopped") and not recording.get("duration_seconds"):
        # Better a slightly short recording window than a manifest the editor refuses outright.
        recording["stopped"] = round(last + 2.0, 3)
    stopped = float(recording.get("stopped") or (anchor + float(recording["duration_seconds"])))
    if stopped <= anchor:
        raise ValueError(
            f"The recording stopped ({stopped}) at or before it started ({anchor}); "
            "the anchor is probably from a different take."
        )

    session = take.get("session") or {}
    session_id = str(session.get("session_id") or "").strip()
    if not session_id:
        raise ValueError(
            "No session id recorded, and the editor seeds its randomised trims from it. Run the "
            "storyboard against a live sandbox, or pass session_id explicitly."
        )

    markers = harvest_markers(session.get("control_dir"), steps)
    run = {
        "schema": RUN_SCHEMA,
        "session_id": session_id,
        "storyboard": take.get("storyboard") or "",
        "recording": {
            "source": recording.get("source") or "",
            "anchor": round(anchor, 3),
            "stopped": round(stopped, 3),
            "duration_seconds": round(stopped - anchor, 3),
            **(
                {"frame_rate": float(recording["frame_rate"])}
                if recording.get("frame_rate")
                else {}
            ),
            "variable_frame_rate": bool(recording.get("variable_frame_rate", True)),
        },
        "steps": steps,
        "markers": markers,
        "cues": [c for c in listed if anchor <= c["wall_end"]],
    }
    if take.get("agenda"):
        run["agenda"] = take["agenda"]
    return run


def _clean_steps(raw: list[Any]) -> list[dict[str, Any]]:
    """Only steps with a real span reach the editor; it refuses overlaps, so they are nudged."""
    steps: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        start, end = entry.get("start_wall"), entry.get("end_wall")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        step: dict[str, Any] = {
            "index": entry.get("step"),
            "action": entry.get("action") or "",
            "start": round(float(start), 3),
            "end": round(float(max(end, start)), 3),
            "ok": bool(entry.get("ok", True)),
        }
        for key in ("chapter", "task", "title"):
            if entry.get(key):
                step[key] = entry[key]
        steps.append(step)
    steps.sort(key=lambda step: step["start"])
    for current, following in zip(steps, steps[1:]):
        if following["start"] < current["end"]:
            current["end"] = round(following["start"] - EPS, 3)
    return [s for s in steps if s["end"] >= s["start"]]


def _clean_cue(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    start, end = raw.get("wall_start"), raw.get("wall_end")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end <= start:
        return None
    cue = {
        "step": raw.get("step"),
        "text": str(raw.get("text") or ""),
        "wall_start": round(float(start), 3),
        "wall_end": round(float(end), 3),
    }
    # Ignored by the editor, but this is also what the caption writer reads back.
    for key in ("id", "kind"):
        if raw.get(key):
            cue[key] = raw[key]
    return cue
