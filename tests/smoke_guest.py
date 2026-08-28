"""Phase 3 acceptance check: guest agent, status channel, markers and real exit codes.

    .venv\\Scripts\\python.exe tests\\smoke_guest.py

Proves the plan's acceptance - run_in_guest(visible=False) returns a real exit code and
await_guest unblocks on a marker - by installing Notepad++ from a clean machine and then
launching it visibly. Nothing may steal focus on the host while this runs.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from sandbox_mcp.server import server  # noqa: E402

NOTEPADPP = r"C:\Program Files\Notepad++\notepad++.exe"


def payload(result) -> dict:
    if getattr(result, "is_error", False):
        raise RuntimeError(result.content[0].text if result.content else "tool call failed")
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


def dump_control(control: Path) -> None:
    print("--- control folder ---")
    for item in sorted(control.rglob("*")):
        if item.is_file():
            print(" ", item.relative_to(control), item.stat().st_size, "bytes")
    for name in ("agent-boot.log", "status.json", "transcript.log"):
        target = control / name
        if target.is_file():
            print(f"--- {name} ---")
            print(target.read_text(encoding="utf-8-sig", errors="replace")[-3000:])


async def main() -> int:
    async with Client(server) as client:
        setup = payload(await client.call_tool("check_sandbox_setup", {}))
        if not setup.get("ready"):
            print("FAIL: host cannot run Windows Sandbox:", setup.get("blockers"))
            return 1
        if setup.get("running_instances"):
            print("FAIL: a Windows Sandbox instance is already running")
            return 1
        if not setup.get("payload_dir"):
            print("FAIL: no payload folder resolved, so no guest agent would start")
            return 1

        launched = payload(await client.call_tool("launch_sandbox", {}))
        print("launched:", launched["session_id"], "payload:", launched["payload_dir"])
        try:
            ready = payload(await client.call_tool("await_desktop", {"timeout": 240}))
            print("guest ready:", ready["status"])

            status = payload(await client.call_tool("guest_status", {}))
            markers = [m["name"] for m in status["markers"]]
            print("agent:", status["agent"], "markers:", markers)
            if "desktop_ready" not in markers:
                print("FAIL: desktop_ready marker missing")
                dump_control(Path(status["control_dir"]))
                return 1
            if not status["agent"].get("alive"):
                print("FAIL: the hidden agent is not reporting a heartbeat")
                dump_control(Path(status["control_dir"]))
                return 1

            # A non-zero code proves the exit code is the guest's, not a fabricated success.
            failed = payload(
                await client.call_tool(
                    "run_in_guest", {"command": "exit 7", "visible": False, "timeout": 120}
                )
            )
            print("exit 7 ->", failed["exit_code"])
            if failed["exit_code"] != 7:
                print("FAIL: expected exit code 7")
                return 1

            echoed = payload(
                await client.call_tool(
                    "run_in_guest",
                    {
                        "command": "Write-Output 'hello from the pump'",
                        "visible": False,
                        "timeout": 120,
                    },
                )
            )
            print("stdout:", echoed["stdout_tail"].strip(), "exit:", echoed["exit_code"])
            if echoed["exit_code"] != 0 or "hello from the pump" not in echoed["stdout_tail"]:
                print("FAIL: the pump did not return stdout")
                return 1

            print("installing Notepad++ off camera ...")
            install = payload(
                await client.call_tool(
                    "run_in_guest",
                    {
                        "command": r"& C:\payload\install-notepadpp.ps1",
                        "visible": False,
                        "timeout": 420,
                    },
                )
            )
            print("install exit:", install["exit_code"])
            if install["exit_code"] != 0:
                print("FAIL: install failed\nstdout:", install["stdout_tail"], "\nstderr:", install["stderr_tail"])
                return 1

            marker = payload(await client.call_tool("await_guest", {"marker": "notepadpp_installed", "timeout": 30}))
            print("marker:", marker)

            focus = payload(await client.call_tool("focus_sandbox", {}))
            print("focus:", focus["input_focus"])
            await client.call_tool("beat", {"seconds": 2.0, "reason": "settle before the visible beat"})

            shown = payload(
                await client.call_tool(
                    "run_in_guest",
                    {"command": f"Start-Process '{NOTEPADPP}'", "visible": True, "timeout": 120},
                )
            )
            print("visible run:", {k: shown[k] for k in ("exit_code", "succeeded", "ok", "input_clean")})
            print("delivered:", repr(shown["delivered"]))
            if not shown["ok"]:
                print("FAIL: the visible command reported a failure")
                return 1
            if not shown["input_clean"]:
                print("FAIL: the guest received something other than the command that was sent - "
                      "something else typed into the session")
                return 1
            await client.call_tool("beat", {"seconds": 4.0, "reason": "Notepad++ is on screen"})

            log = payload(await client.call_tool("read_guest_log", {"tail": 15}))
            print("--- guest transcript tail ---")
            for line in log["lines"]:
                print(" ", line)
        finally:
            torn = payload(await client.call_tool("teardown_sandbox", {}))
            print("teardown:", torn["torn_down"])

        print("PASS: phase 3 acceptance")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
