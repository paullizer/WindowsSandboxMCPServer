"""Phase 1 acceptance check: launch a real sandbox, find its window, verify focus, tear it down.

    .venv\\Scripts\\python.exe tests\\smoke_sandbox.py

Takes about a minute and puts a Windows Sandbox window on screen. Nothing else may steal
focus while it runs.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from sandbox_mcp.server import server  # noqa: E402


def payload(result) -> dict:
    if getattr(result, "is_error", False):
        raise RuntimeError(result.content[0].text if result.content else "tool call failed")
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


async def main() -> int:
    async with Client(server) as client:
        tools = await client.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))

        setup = payload(await client.call_tool("check_sandbox_setup", {}))
        print("setup:", json.dumps(setup, indent=2))
        if not setup.get("ready"):
            print("FAIL: host cannot run Windows Sandbox")
            return 1
        if setup.get("running_instances"):
            print("FAIL: a Windows Sandbox instance is already running")
            return 1

        launched = payload(await client.call_tool("launch_sandbox", {"width": 1600, "height": 900}))
        print("launched:", json.dumps(launched, indent=2))
        try:
            window = launched.get("window") or {}
            if not window.get("handle"):
                print("FAIL: no sandbox window was found")
                return 1
            print(f"window class name (pin this once confirmed): {window.get('class_name')!r}")

            await client.call_tool("beat", {"seconds": 5, "reason": "let the sandbox settle"})

            focused = payload(await client.call_tool("focus_sandbox", {}))
            print("focused:", json.dumps(focused, indent=2))
            if not focused.get("foreground"):
                print("FAIL: focus was not verified")
                return 1

            info = payload(await client.call_tool("sandbox_info", {}))
            print("info:", json.dumps(info, indent=2))
            if not info.get("foreground"):
                print("FAIL: sandbox window is not foreground after focus_sandbox")
                return 1

            client_size = (window.get("client_size") or {})
            print(f"client size: {client_size.get('width')}x{client_size.get('height')}")
        finally:
            torn = payload(await client.call_tool("teardown_sandbox", {}))
            print("teardown:", json.dumps(torn, indent=2))

        print("PASS: phase 1 acceptance")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
