"""Phase 4 acceptance check: replay the example storyboard end to end, unattended.

    .venv\\Scripts\\python.exe tests\\smoke_storyboard.py

Runs the whole clean-Windows-to-working-app take: boot, install Notepad++ from the internet on
camera, open it and type in it. Takes several minutes. Do not use the keyboard or mouse while it
runs - stray keystrokes land in the guest and the run will report input_clean: false.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from sandbox_mcp.server import server  # noqa: E402

STORYBOARD = "example-notepadpp"


def payload(result) -> dict:
    if getattr(result, "is_error", False):
        raise RuntimeError(result.content[0].text if result.content else "tool call failed")
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


async def main() -> int:
    async with Client(server) as client:
        setup = payload(await client.call_tool("check_sandbox_setup", {}))
        if not setup.get("ready"):
            print("FAIL: host cannot run Windows Sandbox:", setup.get("blockers"))
            return 1
        if setup.get("running_instances"):
            print("FAIL: a Windows Sandbox instance is already running")
            return 1

        listed = payload(await client.call_tool("list_storyboards", {}))
        print("storyboards:", [s["name"] for s in listed["storyboards"]])

        dry = payload(await client.call_tool("run_storyboard", {"name": STORYBOARD, "dry_run": True}))
        print(f"dry run: {dry['storyboard']} - {len(dry['steps'])} steps, beat {dry['beat_between']}s")
        if not dry.get("valid"):
            print("FAIL: storyboard did not validate")
            return 1

        launched = payload(await client.call_tool("launch_sandbox", {}))
        print("launched:", launched["session_id"])
        try:
            report = payload(await client.call_tool("run_storyboard", {"name": STORYBOARD}))
            for step in report["steps"]:
                flag = "ok " if step["ok"] else "ERR"
                detail = step.get("detail") if step["ok"] else step.get("error")
                print(f"  {flag} {step['step']:>2} {step['action']:<14} {step['seconds']:>6.1f}s  {detail}")
            print(f"elapsed: {report['elapsed_seconds']}s")

            if not report["completed"]:
                print(f"FAIL: stopped at step {report['failed_step']} ({report['failed_action']}): {report['error']}")
                return 1

            dirty = [
                s for s in report["steps"]
                if s["action"] == "run_in_guest" and s["detail"].get("input_clean") is False
            ]
            if dirty:
                print("FAIL: something else typed into the guest during the take:",
                      [s["detail"].get("delivered") for s in dirty])
                return 1
        finally:
            torn = payload(await client.call_tool("teardown_sandbox", {}))
            print("teardown:", torn["torn_down"])

        print("PASS: phase 4 acceptance")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
