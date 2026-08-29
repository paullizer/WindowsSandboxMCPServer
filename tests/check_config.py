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

from sandbox_mcp.config import Settings, load_env_file  # noqa: E402
from sandbox_mcp.sandbox import SandboxDriver, SandboxError  # noqa: E402


def check_env_file() -> list[str]:
    """A .env has to survive slashes, colons, quotes and comments, and never beat a real export."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / ".env"
        path.write_text(
            "# a comment\n"
            "\n"
            "AZURE_SPEECH_RESOURCE_ID=/subscriptions/abc/resourceGroups/rg/providers/x/accounts/y\n"
            'AZURE_SPEECH_ENDPOINT="https://example.cognitiveservices.azure.com/"\n'
            "export AZURE_SPEECH_REGION=westus\n"
            "MALFORMED_LINE_WITHOUT_EQUALS\n"
            "CHECK_ENV_ALREADY_SET=from-file\n",
            encoding="utf-8",
        )
        keys = [
            "AZURE_SPEECH_RESOURCE_ID",
            "AZURE_SPEECH_ENDPOINT",
            "AZURE_SPEECH_REGION",
            "CHECK_ENV_ALREADY_SET",
            "SANDBOX_MCP_ENV_FILE",
        ]
        saved = {key: os.environ.get(key) for key in keys}
        for key in keys:
            os.environ.pop(key, None)
        os.environ["SANDBOX_MCP_ENV_FILE"] = str(path)
        os.environ["CHECK_ENV_ALREADY_SET"] = "from-environment"
        try:
            if load_env_file() != path:
                failures.append("SANDBOX_MCP_ENV_FILE was not honoured")
            resource = os.environ.get("AZURE_SPEECH_RESOURCE_ID", "")
            if not resource.startswith("/subscriptions/") or resource.count("/") != 8:
                failures.append(f"slashes in a resource id did not survive: {resource!r}")
            if os.environ.get("AZURE_SPEECH_ENDPOINT") != "https://example.cognitiveservices.azure.com/":
                failures.append("quotes were not stripped, or the colon broke the value")
            if os.environ.get("AZURE_SPEECH_REGION") != "westus":
                failures.append("`export ` prefix was not handled")
            if os.environ.get("CHECK_ENV_ALREADY_SET") != "from-environment":
                failures.append("the .env overrode a variable that was already exported")
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return failures


def main() -> int:
    failures: list[str] = []
    failures += check_env_file()
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
