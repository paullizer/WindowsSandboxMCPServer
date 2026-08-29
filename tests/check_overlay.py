"""Drives the on-camera overlay on the host, so its layout can be designed without a sandbox cycle.

    .venv\\Scripts\\python.exe tests\\check_overlay.py [--shots DIR] [--hold 1.2]

Runs payload\\lib\\overlay.ps1 against a throwaway control folder via SANDBOX_CTL and walks a sample
agenda through it using the real GuestChannel, so this exercises the same files a run publishes.
The overlay is borderless, click-through and never focused, so it draws over the desktop without
taking it over - but it does draw over the desktop, so expect to see it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sandbox_mcp.guest import GuestChannel  # noqa: E402
from sandbox_mcp.storyboard import step_positions  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OVERLAY = REPO / "payload" / "lib" / "overlay.ps1"

AGENDA = {
    "title": "Deploying to Azure from a clean Windows machine",
    "subtitle": "Windows Sandbox - nothing installed, nothing left behind",
    "show_tasks": True,
    "chapters": [
        {"index": 1, "title": "Start from nothing", "tasks": ["Prove it is empty"]},
        {"index": 2, "title": "Install the toolchain",
         "tasks": ["Fetch and install", "Confirm the versions"]},
        {"index": 3, "title": "Clone and sign in", "tasks": ["Clone", "az login", "azd auth"]},
        {"index": 4, "title": "Answer azd's questions", "tasks": ["Name", "Services", "Model"]},
        {"index": 5, "title": "See where it landed", "tasks": []},
    ],
}

# Enough of a run to put the bar through every state it can be in.
CHAPTERS = [
    {"index": 1, "title": "Start from nothing", "first_step": 1, "last_step": 2,
     "tasks": [{"index": 1, "title": "Prove it is empty", "first_step": 1, "last_step": 2}]},
    {"index": 2, "title": "Install the toolchain", "first_step": 3, "last_step": 6,
     "tasks": [{"index": 1, "title": "Fetch and install", "first_step": 3, "last_step": 4},
               {"index": 2, "title": "Confirm the versions", "first_step": 5, "last_step": 6}]},
    {"index": 3, "title": "Clone and sign in", "first_step": 7, "last_step": 9,
     "tasks": [{"index": 1, "title": "Clone", "first_step": 7, "last_step": 7},
               {"index": 2, "title": "az login", "first_step": 8, "last_step": 8},
               {"index": 3, "title": "azd auth", "first_step": 9, "last_step": 9}]},
    {"index": 4, "title": "Answer azd's questions", "first_step": 10, "last_step": 12,
     "tasks": [{"index": 1, "title": "Name", "first_step": 10, "last_step": 10},
               {"index": 2, "title": "Services", "first_step": 11, "last_step": 11},
               {"index": 3, "title": "Model", "first_step": 12, "last_step": 12}]},
    {"index": 5, "title": "See where it landed", "first_step": 13, "last_step": 13, "tasks": []},
]

_SHOT = """param([string]$Path, [int]$X, [int]$Y, [int]$Width, [int]$Height)
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap($Width, $Height)
$canvas = [System.Drawing.Graphics]::FromImage($bitmap)
$canvas.CopyFromScreen($X, $Y, 0, 0, $bitmap.Size)
$bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
"""

BAR_HEIGHT = 88


class Camera:
    """Screen grabs of the region the overlay was told to draw in."""

    def __init__(self, target: Path | None, bounds: tuple[int, int, int, int], script: Path):
        self.target = target
        self.bounds = bounds
        self.script = script

    def __call__(self, name: str, region: str) -> None:
        if self.target is None:
            return
        x, y, width, height = self.bounds
        if region == "bar":
            y, height = y + height - BAR_HEIGHT, BAR_HEIGHT
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(self.script),
             str(self.target / f"{name}.png"), str(x), str(y), str(width), str(height)],
            check=True,
            capture_output=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", default="", help="write a PNG of each state to this folder")
    parser.add_argument("--hold", type=float, default=1.2, help="seconds to hold each position")
    parser.add_argument(
        "--size", default="1600x900", help="render at the sandbox's window size, not the host's"
    )
    args = parser.parse_args()
    shots = Path(args.shots).expanduser().resolve() if args.shots else None
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)
    width, _, height = args.size.partition("x")
    bounds = (0, 0, int(width), int(height))

    with tempfile.TemporaryDirectory() as raw:
        control = Path(raw)
        channel = GuestChannel(control)
        script = control / "shot.ps1"
        script.write_text(_SHOT, encoding="utf-8")
        shot = Camera(shots, bounds, script)
        process = subprocess.Popen(
            ["powershell.exe", "-ExecutionPolicy", "Bypass", "-NoProfile", "-File", str(OVERLAY)],
            env={
                **os.environ,
                "SANDBOX_CTL": str(control),
                "SANDBOX_OVERLAY_BOUNDS": ",".join(str(v) for v in bounds),
            },
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            channel.wait_for_marker("overlay_ready", 90)
            print("ready:", (channel.marker("overlay_ready") or {}).get("detail"))

            channel.publish_agenda(AGENDA)
            channel.publish_overlay(mode="agenda", chapter=0, task=0, status="running")
            time.sleep(3.0)
            shot("01-agenda-card", "full")
            channel.publish_overlay(mode="timeline")

            positions = step_positions(CHAPTERS)
            for index in sorted(positions):
                channel.publish_overlay(status="running", note="", **positions[index])
                time.sleep(args.hold)
                shot(f"02-step-{index:02d}", "bar")

            channel.publish_overlay(status="waiting", note="waiting for you to sign in")
            time.sleep(args.hold)
            shot("03-waiting", "bar")

            channel.publish_overlay(status="done", note="")
            time.sleep(args.hold)
            shot("04-done", "bar")
        finally:
            process.terminate()
            process.wait(timeout=10)

    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
