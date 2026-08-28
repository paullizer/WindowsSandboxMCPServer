"""Reattach guard checks. Read-only: finds windows and reads control folders, never drives the guest.

Needs a sandbox launched by this server to be running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sandbox_mcp.config import Settings  # noqa: E402
from sandbox_mcp.sandbox import SandboxDriver, SandboxError  # noqa: E402

REPO_SESSIONS = Path(__file__).resolve().parents[1] / "sessions"


def attempt(work_dir: Path | None) -> tuple[bool, str]:
    if work_dir is None:
        os.environ.pop("SANDBOX_MCP_WORK_DIR", None)
    else:
        os.environ["SANDBOX_MCP_WORK_DIR"] = str(work_dir)
    driver = SandboxDriver(Settings.from_env())
    try:
        session = driver.reattach()
    except SandboxError as exc:
        return False, str(exc)
    return True, f"{session.session_id} client={driver.info()['window']['client_size']}"


def main() -> int:
    failures = []

    ok, detail = attempt(REPO_SESSIONS)
    if ok:
        print(f"OK  adopted live session: {detail}")
    elif "No running sandbox session window" in detail:
        print("SKIP no sandbox is running; launch one to exercise reattach")
        return 0
    else:
        print(f"FAIL could not adopt the running session: {detail}")
        failures.append("live")

    # The default work_dir holds only long-dead sessions, whose agent.json still says alive:true.
    ok, detail = attempt(None)
    if ok:
        print(f"FAIL adopted a stale control folder: {detail}")
        failures.append("stale")
    else:
        print(f"OK  rejected stale control folder: {detail.splitlines()[0][:120]}")

    ok, detail = attempt(REPO_SESSIONS / "does-not-exist")
    if ok:
        print(f"FAIL adopted a session from an empty root: {detail}")
        failures.append("empty")
    else:
        print(f"OK  rejected empty root: {detail.splitlines()[0][:120]}")

    print("PASS" if not failures else f"FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
