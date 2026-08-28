r"""Configuration and first-run checks. Pure host-side; never launches Windows Sandbox.

    .venv\Scripts\python.exe tests\check_config.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sandbox_mcp.config import Settings  # noqa: E402
from sandbox_mcp.sandbox import SandboxDriver, SandboxError  # noqa: E402


def main() -> int:
    failures: list[str] = []
    settings = Settings.from_env()

    if settings.payload_dir is None or not (settings.payload_dir / "bootstrap.ps1").is_file():
        failures.append(f"default payload was not resolved: {settings.payload_dir}")
    if settings.storyboard_dir is None or not settings.list_storyboards():
        failures.append(f"default storyboards were not resolved: {settings.storyboard_dir}")

    with tempfile.TemporaryDirectory() as raw_dir:
        empty = Path(raw_dir)
        driver = SandboxDriver(replace(settings, payload_dir=empty))
        try:
            driver._resolve_payload_dir(None)
        except SandboxError as exc:
            if "bootstrap.ps1" not in str(exc):
                failures.append(f"incomplete payload error was not actionable: {exc}")
        else:
            failures.append("incomplete payload folder was accepted")

    missing = replace(settings, payload_dir=None)
    driver = SandboxDriver(missing)
    try:
        driver._resolve_payload_dir(None)
    except SandboxError as exc:
        if "SANDBOX_MCP_PAYLOAD_DIR" not in str(exc):
            failures.append(f"missing payload error was not actionable: {exc}")
    else:
        failures.append("missing payload folder was accepted")

    if os.environ.get("SANDBOX_MCP_PAYLOAD_DIR"):
        print("NOTE using SANDBOX_MCP_PAYLOAD_DIR override")
    print("PASS" if not failures else f"FAIL ({len(failures)}): {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
