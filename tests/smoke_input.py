"""Phase 2 acceptance check: open an app in the guest by keyboard alone, then type and paste.

    .venv\\Scripts\\python.exe tests\\smoke_input.py

The plan called for Notepad, but the Windows Sandbox image ships without it (no Notepad, WordPad,
Paint or Edge), so this opens PowerShell instead. Verified without screenshots: the guest writes
what it received into the mapped control folder and the host compares it byte for byte.

Takes a couple of minutes, puts a sandbox window on screen, and nothing else may steal focus.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp import Client  # noqa: E402

from sandbox_mcp.server import server  # noqa: E402

BOOT_SECONDS = float(os.environ.get("SANDBOX_MCP_SMOKE_BOOT_SECONDS", "60"))
# Quoting survives the run box badly, so the launcher lives in the mapped folder as a script.
SHELL_SCRIPT = "Set-Content -Path C:\\ctl\\shell_ready.txt -Value 'ready'\r\n"
LAUNCH_SHELL = "powershell -ExecutionPolicy Bypass -NoProfile -NoExit -File C:\\ctl\\shell.ps1"
TYPED = "typed by SendInput scancodes"
PASTED = "pasted through the clipboard"
TYPED_CMD = f"'{TYPED}' | Set-Content C:\\ctl\\typed.txt"
PASTED_CMD = f"'{PASTED}' | Set-Content C:\\ctl\\pasted.txt"


def payload(result) -> dict:
    if getattr(result, "is_error", False):
        raise RuntimeError(result.content[0].text if result.content else "tool call failed")
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


def wait_for_file(path: Path, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.4)
    return None


async def expect_refusal(client, tool: str, args: dict) -> None:
    result = await client.call_tool(tool, args)
    if not getattr(result, "is_error", False):
        raise AssertionError(f"{tool} should have refused {args!r}")
    print(f"{tool} refused a credential as expected")


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
        (control / "shell.ps1").write_text(SHELL_SCRIPT, encoding="utf-8")
        print("launched:", launched["session_id"], launched["window"]["client_size"])
        try:
            await client.call_tool(
                "beat", {"seconds": BOOT_SECONDS, "reason": "guest desktop is booting"}
            )
            focus = payload(await client.call_tool("focus_sandbox", {}))
            print("focus:", focus["foreground"], focus["input_focus"], focus["resized_to"])
            await client.call_tool("beat", {"seconds": 4.0, "reason": "guest renegotiates resolution"})

            # Win+R rather than start-menu search: the run box is always present in a fresh guest.
            ready = None
            for attempt in range(2):
                await client.call_tool("send_keys", {"chord": "win+r"})
                await client.call_tool("beat", {"seconds": 2.5, "reason": "run box opens"})
                await client.call_tool("paste_text", {"text": LAUNCH_SHELL})
                await client.call_tool("beat", {"seconds": 1.0, "reason": "read the command"})
                await client.call_tool("send_keys", {"chord": "enter"})
                ready = wait_for_file(control / "shell_ready.txt", 40)
                if ready is not None:
                    break
                print(f"attempt {attempt + 1}: no shell yet, control={[p.name for p in control.iterdir()]}")
            if ready is None:
                print("FAIL: PowerShell never reported a prompt in the guest")
                return 1
            print("guest shell is up")
            await client.call_tool("beat", {"seconds": 1.5, "reason": "settle at the prompt"})

            typed = payload(await client.call_tool("type_text", {"text": TYPED_CMD + "\n", "cps": 18}))
            print("typed:", typed)
            typed_back = wait_for_file(control / "typed.txt", 30)
            if typed_back != TYPED:
                print(f"FAIL: guest received {typed_back!r} from type_text, expected {TYPED!r}")
                return 1
            print("type_text round-tripped exactly")

            pasted = payload(await client.call_tool("paste_text", {"text": PASTED_CMD}))
            print("pasted:", pasted)
            await client.call_tool("beat", {"seconds": 1.0, "reason": "the pasted command is readable"})
            await client.call_tool("send_keys", {"chord": "enter"})
            pasted_back = wait_for_file(control / "pasted.txt", 30)
            if pasted_back != PASTED:
                print(f"FAIL: guest received {pasted_back!r} from paste_text, expected {PASTED!r}")
                return 1
            print("paste_text round-tripped exactly")

            await expect_refusal(client, "type_text", {"text": "password = hunter2"})
            await expect_refusal(client, "paste_text", {"text": "ghp_abcdefghijklmnopqrstuvwxyz012345"})

            moved = payload(await client.call_tool("move_mouse", {"x": 800, "y": 450}))
            print("move_mouse:", moved["screen"])
        finally:
            torn = payload(await client.call_tool("teardown_sandbox", {}))
            print("teardown:", torn["torn_down"])

        print("PASS: phase 2 acceptance")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
