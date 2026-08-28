"""Phase 5 acceptance check: the sandbox and Snagit servers composed into a recorded demo.

    .venv\\Scripts\\python.exe tests\\smoke_recording.py

Boots a clean sandbox off camera, then records the storyboard replay to an MP4. The Snagit server
is imported from the sibling LocalSnagitMCPServer checkout; point SNAGIT_MCP_SRC elsewhere if it
lives somewhere else.

Takes several minutes and records the real screen. Turn on Do Not Disturb, and do not use the
keyboard or mouse - stray input lands in the guest and ends up in the video.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

RECORDINGS = REPO / "recordings"
RECORDINGS.mkdir(exist_ok=True)
# Snagit's Settings.from_env() runs at import, so this has to be set first.
os.environ.setdefault("SNAGIT_MCP_OUTPUT_DIR", str(RECORDINGS))

SNAGIT_SRC = Path(
    os.environ.get("SNAGIT_MCP_SRC") or (REPO.parent / "LocalSnagitMCPServer" / "src")
)
if not (SNAGIT_SRC / "snagit_mcp").is_dir():
    raise SystemExit(
        f"Snagit MCP server not found at {SNAGIT_SRC}. Clone LocalSnagitMCPServer beside this "
        "repo, or set SNAGIT_MCP_SRC to its src directory."
    )
sys.path.insert(0, str(SNAGIT_SRC))

from mcp import Client  # noqa: E402

from sandbox_mcp.server import server as sandbox_server  # noqa: E402
from snagit_mcp.server import server as snagit_server  # noqa: E402

STORYBOARD = "example-notepadpp"
FILE_NAME = "notepadpp-from-clean-windows"


def payload(result) -> dict:
    if getattr(result, "is_error", False):
        raise RuntimeError(result.content[0].text if result.content else "tool call failed")
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


async def main() -> int:
    async with Client(sandbox_server) as sandbox, Client(snagit_server) as camera:
        setup = payload(await sandbox.call_tool("check_sandbox_setup", {}))
        if not setup.get("ready"):
            print("FAIL: host cannot run Windows Sandbox:", setup.get("blockers"))
            return 1
        if setup.get("running_instances"):
            print("FAIL: a Windows Sandbox instance is already running")
            return 1

        snagit = payload(await camera.call_tool("check_snagit_setup", {}))
        if not snagit.get("ready"):
            print("FAIL: Snagit is not reachable over COM:", snagit.get("com_error"))
            return 1
        print(f"camera: Snagit {snagit['snagit_version']} -> {snagit['output_directory']}")

        launched = payload(await sandbox.call_tool("launch_sandbox", {}))
        print("launched:", launched["session_id"])
        recording_started = False
        try:
            # Boot and framing happen before the camera rolls: the wait is dead air, and
            # focus_sandbox re-pins the window, which would move the shot mid-take.
            await sandbox.call_tool("await_desktop", {"timeout": 240})
            focus = payload(await sandbox.call_tool("focus_sandbox", {}))
            print("guest ready, window pinned:", focus["window"]["client_size"])

            info = payload(await sandbox.call_tool("sandbox_info", {}))
            handle = info["window"]["handle"]

            started = payload(
                await camera.call_tool(
                    "start_recording",
                    {
                        "target": "window",
                        "window_handle": handle,
                        "file_name": FILE_NAME,
                        "include_cursor": True,
                    },
                )
            )
            recording_started = True
            print("recording:", started["output_path"])

            report = payload(await sandbox.call_tool("run_storyboard", {"name": STORYBOARD}))
            for step in report["steps"]:
                flag = "ok " if step["ok"] else "ERR"
                detail = step.get("detail") if step["ok"] else step.get("error")
                print(f"  {flag} {step['step']:>2} {step['action']:<14} {step['seconds']:>6.1f}s  {detail}")

            stopped = payload(await camera.call_tool("stop_recording", {}))
            recording_started = False
            print("stopped:", json.dumps(stopped, indent=2))

            if not report["completed"]:
                print(f"FAIL: storyboard stopped at step {report['failed_step']}: {report['error']}")
                return 1
            dirty = [
                s for s in report["steps"]
                if s["action"] == "run_in_guest" and s["detail"].get("input_clean") is False
            ]
            if dirty:
                print("FAIL: something else typed into the guest, the take is spoilt")
                return 1

            if not stopped.get("succeeded") or stopped.get("recorder_error") != "none":
                print("FAIL: Snagit reported a recorder error")
                return 1
            if not stopped.get("file_exists") or stopped.get("file_size_bytes", 0) < 200_000:
                print("FAIL: the MP4 is missing or suspiciously small")
                return 1
            duration = stopped.get("snagit_duration_seconds", 0)
            if duration < 30:
                print(f"FAIL: recording is only {duration}s, expected the whole replay")
                return 1

            print(
                f"\nvideo: {stopped['output_path']}\n"
                f"  {duration}s, {stopped['file_size_bytes'] / 1_000_000:.1f} MB, "
                f"{stopped['average_frame_rate']} fps, {stopped['frame_count']} frames"
            )
        finally:
            if recording_started:
                await camera.call_tool("stop_recording", {"discard": True})
            torn = payload(await sandbox.call_tool("teardown_sandbox", {}))
            print("teardown:", torn["torn_down"])

        print("PASS: phase 5 acceptance")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
