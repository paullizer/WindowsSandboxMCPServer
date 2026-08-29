r"""Phase 8 acceptance check: the run manifest, and whether the video editor will accept it.

    .venv\Scripts\python.exe tests\check_manifest.py

Host-side only - no sandbox, no camera, no video. Builds run.json from a synthetic take and checks
the shape the editor depends on.

The last check is the one that matters: it imports LocalVideoEditMCPServer and feeds our manifest to
its real plan_cut, so the contract is verified against the actual consumer rather than a guess.
Point VIDEO_EDIT_MCP_SRC at that checkout's src directory; the check skips cleanly without it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sandbox_mcp import timeline  # noqa: E402
from sandbox_mcp.config import Settings  # noqa: E402

ANCHOR = 1787821597.0
# Deliberately mirrors fixtures/synthetic-take.json in the editor repo: a step that reports
# progress and so can be trimmed, and one that stays silent and therefore cannot.
STEPS = [
    {"step": 1, "action": "beat", "chapter": 1, "task": "Set the scene", "title": "Set the scene",
     "start_wall": ANCHOR + 4.0, "end_wall": ANCHOR + 12.5, "ok": True},
    {"step": 2, "action": "run_in_guest", "chapter": 1, "task": "Prove it is empty",
     "title": "Prove it is empty",
     "start_wall": ANCHOR + 20.5, "end_wall": ANCHOR + 41.2, "ok": True},
    {"step": 3, "action": "run_in_guest", "chapter": 2, "task": "Install", "title": "Install",
     "start_wall": ANCHOR + 118.9, "end_wall": ANCHOR + 820.0, "ok": True},
    {"step": 4, "action": "run_in_guest", "chapter": 3, "task": "Deploy", "title": "Deploy",
     "start_wall": ANCHOR + 852.5, "end_wall": ANCHOR + 1302.7, "ok": True},
]
CUES = [
    {"id": "s1-say", "kind": "say", "step": 1, "text": "A clean machine.",
     "wall_start": ANCHOR + 4.5, "wall_end": ANCHOR + 11.8},
    # Spills past its step into the following gap: the editor's narration floor depends on this
    # surviving export uncut, so it must not be clipped to the step.
    {"id": "s2-say", "kind": "say", "step": 2, "text": "Nothing is installed on it.",
     "wall_start": ANCHOR + 38.0, "wall_end": ANCHOR + 46.5},
    {"id": "s4-say", "kind": "say", "step": 4, "text": "This provisions real infrastructure.",
     "wall_start": ANCHOR + 853.0, "wall_end": ANCHOR + 878.5},
]
MARKERS = {
    "provision-started": ANCHOR + 869.0,
    "provision-complete": ANCHOR + 1215.4,
}


def seed(directory: Path) -> tuple[Settings, dict[str, Any]]:
    settings = replace(Settings.from_env(), work_dir=directory / "work")
    control = directory / "control"
    (control / "markers").mkdir(parents=True, exist_ok=True)
    for name, at in MARKERS.items():
        stamp = datetime.fromtimestamp(at, timezone(timedelta(hours=-4)))
        # PowerShell's Get-Date -Format o writes seven fractional digits.
        ts = stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond:06d}0" + stamp.strftime("%z")
        ts = f"{ts[:-2]}:{ts[-2:]}"
        (control / "markers" / f"{name}.json").write_text(
            json.dumps({"name": name, "detail": "", "ts": ts}), encoding="utf-8"
        )
    take = {
        "storyboard": "check-manifest",
        "agenda": {"title": "A take", "subtitle": "", "show_tasks": True, "chapters": [
            {"index": 1, "title": "Start from nothing", "tasks": ["Set the scene"]},
            {"index": 2, "title": "Install", "tasks": ["Install"]},
            {"index": 3, "title": "Deploy", "tasks": ["Deploy"]},
        ]},
        "session": {"session_id": "sbx-20260829-101500", "control_dir": str(control)},
        "recording": {"source": "recordings/take.mp4", "anchor": ANCHOR, "stopped": ANCHOR + 1310.0},
        "steps": list(STEPS),
    }
    return settings, take


def check_shape(settings: Settings, take: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    run = timeline.build_run(settings, take, CUES)

    if run["schema"] != "sandbox-run/1":
        failures.append(f"wrong schema: {run['schema']}")
    if run["session_id"] != "sbx-20260829-101500":
        failures.append("session id did not survive; the editor seeds its trims from it")

    recording = run["recording"]
    if recording["anchor"] != ANCHOR or recording["duration_seconds"] != 1310.0:
        failures.append(f"recording window wrong: {recording}")

    steps = run["steps"]
    if [s["index"] for s in steps] != [1, 2, 3, 4]:
        failures.append(f"steps lost their indices: {[s.get('index') for s in steps]}")
    for current, following in zip(steps, steps[1:]):
        if following["start"] < current["end"]:
            failures.append("steps overlap, which the editor refuses outright")
    if steps[0].get("chapter") != 1 or steps[0].get("title") != "Set the scene":
        failures.append(f"agenda placement missing from the step: {steps[0]}")

    named = {m["name"]: m for m in run["markers"]}
    if set(named) != set(MARKERS):
        failures.append(f"markers lost: {sorted(named)}")
    for name, at in MARKERS.items():
        got = named.get(name, {}).get("at")
        if got is None or abs(got - at) > 0.01:
            failures.append(f"marker {name} timed at {got}, expected {at} (ISO parsing?)")
    if named.get("provision-started", {}).get("step") != 4:
        failures.append(
            f"marker not attributed to the step containing it: {named.get('provision-started')}"
        )

    spill = next((c for c in run["cues"] if c["id"] == "s2-say"), None)
    if spill is None or spill["wall_end"] != ANCHOR + 46.5:
        failures.append("a cue that runs past its step was clipped; the narration floor needs it")
    if len(run["cues"]) != len(CUES):
        failures.append(f"expected {len(CUES)} cues, got {len(run['cues'])}")
    return failures, run


def check_refusals(settings: Settings, take: dict[str, Any]) -> list[str]:
    """A manifest the editor would reject should be refused here, where the message can be useful."""
    failures: list[str] = []
    cases = {
        "anchor": ({**take, "recording": {"source": "x.mp4"}}, "note_recording"),
        "steps": ({**take, "steps": []}, "Run the storyboard"),
        "session": ({**take, "session": {}}, "session id"),
    }
    for label, (broken, expected) in cases.items():
        try:
            timeline.build_run(settings, broken, CUES)
        except ValueError as exc:
            if expected not in str(exc):
                failures.append(f"{label}: message was not actionable: {exc}")
        else:
            failures.append(f"{label}: a manifest missing its {label} was accepted")

    backwards = {**take, "recording": {**take["recording"], "stopped": ANCHOR - 5}}
    try:
        timeline.build_run(settings, backwards, CUES)
    except ValueError:
        pass
    else:
        failures.append("a recording that stopped before it started was accepted")
    return failures


def check_overlap_repair(settings: Settings, take: dict[str, Any]) -> list[str]:
    """Steps are sequential in practice, but a resumed segment can re-report one."""
    doubled = {**take, "steps": STEPS + [dict(STEPS[1])]}
    run = timeline.build_run(settings, doubled, CUES)
    for current, following in zip(run["steps"], run["steps"][1:]):
        if following["start"] < current["end"]:
            return ["duplicate steps produced an overlap the editor would refuse"]
    return []


def check_editor_accepts(run: dict[str, Any], directory: Path) -> list[str]:
    src = Path(os.environ.get("VIDEO_EDIT_MCP_SRC") or (REPO.parent / "LocalVideoEditMCPServer" / "src"))
    if not (src / "video_edit_mcp").is_dir():
        print(f"SKIP editor contract check: no video_edit_mcp under {src}")
        return []

    sys.path.insert(0, str(src))
    try:
        from video_edit_mcp.config import Settings as EditSettings
        from video_edit_mcp.cut import CutError, plan_cut
    except ImportError as exc:
        print(f"SKIP editor contract check: {exc}")
        return []

    inputs = directory / "editor"
    inputs.mkdir(parents=True, exist_ok=True)
    path = inputs / "run.json"
    path.write_text(json.dumps(run, indent=2), encoding="utf-8")

    saved = {k: os.environ.get(k) for k in ("VIDEO_EDIT_MCP_WORK_DIR", "VIDEO_EDIT_MCP_INPUT_DIRS")}
    os.environ["VIDEO_EDIT_MCP_WORK_DIR"] = str(inputs)
    os.environ["VIDEO_EDIT_MCP_INPUT_DIRS"] = str(inputs)
    try:
        cut = plan_cut(path, source="take.mp4", dry_run=True, settings=EditSettings.from_env())
    except CutError as exc:
        return [f"the video editor refused our manifest: {exc}"]
    except Exception as exc:  # noqa: BLE001 - any failure here is a contract failure
        return [f"the video editor blew up on our manifest: {type(exc).__name__}: {exc}"]
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    segments = cut.get("segments") or []
    if not segments:
        return ["the editor planned no segments from our manifest"]
    kept = cut.get("output_duration", 0.0)
    if not 0 < kept < cut.get("source_duration", 0.0):
        return [f"implausible edit: {kept}s out of {cut.get('source_duration')}s"]
    print(
        f"  editor: {len(segments)} segments, {cut['source_duration']}s -> {kept}s "
        f"({cut['summary'] if isinstance(cut.get('summary'), str) else 'planned'})"
    )
    return []


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        settings, take = seed(directory)

        shape, run = check_shape(settings, take)
        failures += shape
        failures += check_refusals(settings, take)
        failures += check_overlap_repair(settings, take)

        # Round-trips through disk the way the server does it.
        timeline.save_take(settings, take)
        if timeline.load_take(settings).get("session") != take["session"]:
            failures.append("take state did not survive a save/load round trip")

        failures += check_editor_accepts(run, directory)

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("OK: run manifest checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
