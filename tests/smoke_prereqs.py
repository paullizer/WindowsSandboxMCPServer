"""Validate the prerequisite installer against a clean sandbox.

    .venv\\Scripts\\python.exe tests\\smoke_prereqs.py

Everything runs through the hidden agent, so no synthetic input is sent and the host stays
usable while it works. Budget ten minutes or so: roughly 265 MB of installers.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from sandbox_mcp.server import server  # noqa: E402

TIMEOUT = 1500.0


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

        launched = payload(await client.call_tool("launch_sandbox", {}))
        control = Path(launched["control_dir"])
        print("launched:", launched["session_id"])
        try:
            payload(await client.call_tool("await_desktop", {"timeout": 240}))
            print("guest ready, installing prerequisites (hidden) ...")

            result = payload(
                await client.call_tool(
                    "run_in_guest",
                    {
                        "command": r"& C:\payload\install-prereqs.ps1",
                        "visible": False,
                        "timeout": TIMEOUT,
                    },
                )
            )
            print("exit code:", result["exit_code"])
            if result["exit_code"] != 0:
                print("--- stdout ---\n", result["stdout_tail"])
                print("--- stderr ---\n", result["stderr_tail"])
                print("FAIL: prerequisite install failed")
                return 1

            status = payload(await client.call_tool("guest_status", {}))
            markers = [m["name"] for m in status["markers"]]
            print("markers:", markers)

            versions = json.loads((control / "prereqs.json").read_text(encoding="utf-8-sig"))
            print("--- installed ---")
            for name, version in versions.items():
                print(f"  {name:<7} {version}")
            if any(v in (None, "") for v in versions.values()):
                print("FAIL: a prerequisite is missing")
                return 1

            print("--- install log tail ---")
            for line in result["stdout_tail"].splitlines()[-15:]:
                print(" ", line)
        finally:
            torn = payload(await client.call_tool("teardown_sandbox", {}))
            print("teardown:", torn["torn_down"])

        print("PASS: prerequisites install on a clean machine")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
