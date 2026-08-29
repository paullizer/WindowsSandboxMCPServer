"""Storyboard parser checks. Pure host-side, no sandbox required.

    .venv\\Scripts\\python.exe tests\\check_storyboard.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sandbox_mcp import storyboard  # noqa: E402
from sandbox_mcp.config import Settings  # noqa: E402

BAD = {
    "unknown action": "steps:\n  - teleport: home\n",
    "multi-key step": "steps:\n  - send_keys: win\n    beat: 1\n",
    "missing argument": "steps:\n  - click: {x: 10}\n",
    "unknown argument": "steps:\n  - type_text: {text: hi, speed: 3}\n",
    "scalar where mapping needed": "steps:\n  - click: 10\n",
    "no steps": "name: empty\n",
}


def check_companion_values() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw_dir:
        directory = Path(raw_dir)
        child = directory / "child.yaml"
        child.write_text(
            "steps:\n  - run_in_guest:\n      command: 'deploy --id {{azure.subscription_id}}'\n"
            "      visible: '{{options.visible}}'\n",
            encoding="utf-8",
        )
        root = directory / "root.yaml"
        root.write_text("steps:\n  - include: child\n", encoding="utf-8")
        example = directory / storyboard.VALUES_EXAMPLE_FILE
        example.write_text(
            json.dumps({"azure": {"subscription_id": "example-id"}, "options": {"visible": False}}),
            encoding="utf-8",
        )

        def resolve(name: str) -> Path:
            return directory / f"{name}.yaml"

        board = storyboard.load(root, resolve)
        if board.steps[0].args != {"command": "deploy --id example-id", "visible": False}:
            failures.append(f"companion values resolved incorrectly: {board.steps[0].args}")
        if board.configured or board.required_values != (
            "azure.subscription_id",
            "options.visible",
        ):
            failures.append("example values were not marked as non-runnable")
        try:
            storyboard.run(board, object())
        except storyboard.StoryboardError as exc:
            if storyboard.VALUES_FILE not in str(exc):
                failures.append(f"configuration error was not actionable: {exc}")
        else:
            failures.append("a storyboard using example values should not run")

        actual = directory / storyboard.VALUES_FILE
        actual.write_text(
            json.dumps({"azure": {"subscription_id": "local-id"}, "options": {"visible": True}}),
            encoding="utf-8",
        )
        configured = storyboard.load(root, resolve)
        if not configured.configured:
            failures.append("local values were not marked as runnable")
        if configured.steps[0].args != {"command": "deploy --id local-id", "visible": True}:
            failures.append(f"local values resolved incorrectly: {configured.steps[0].args}")

    return failures


def check_agenda() -> list[str]:
    """An agenda has to survive an include, or a stitched-together run cannot state one plan."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw_dir:
        directory = Path(raw_dir)
        (directory / "child.yaml").write_text(
            "steps:\n"
            "  - beat: {seconds: 0, chapter: Install, task: Fetch}\n"
            "  - beat: 0\n"
            "  - beat: {seconds: 0, task: Verify}\n",
            encoding="utf-8",
        )
        root = directory / "root.yaml"
        root.write_text(
            "agenda: {title: A run, subtitle: with two parts}\n"
            "steps:\n"
            "  - include: child\n"
            "  - beat: {seconds: 0, chapter: Deploy}\n",
            encoding="utf-8",
        )

        board = storyboard.load(root, lambda name: directory / f"{name}.yaml")
        agenda = board.agenda()
        titles = [c["title"] for c in agenda["chapters"]]
        if titles != ["Install", "Deploy"]:
            failures.append(f"include did not fold its chapters into the parent agenda: {titles}")
        if agenda["title"] != "A run" or agenda["subtitle"] != "with two parts":
            failures.append(f"agenda header was lost: {agenda}")
        if agenda["chapters"][0]["tasks"] != ["Fetch", "Verify"]:
            failures.append(f"tasks did not survive the include: {agenda['chapters'][0]}")

        chapters = board.chapters()
        if chapters[0]["tasks"][0]["last_step"] != 2:
            failures.append("an unlabelled step did not extend the task it runs inside")

        positions = storyboard.step_positions(chapters)
        if positions[1]["chapter_next"] != 0.5 or positions[3]["chapter_progress"] != 0.5:
            failures.append(f"chapter progress did not track its tasks: {positions}")
        if positions[4]["chapter"] != 2 or positions[4]["task"] != 0:
            failures.append(f"a chapter without tasks was mispositioned: {positions[4]}")

        published: list[tuple[Any, ...]] = []

        class Recorder:
            def publish_agenda(self, agenda: dict[str, Any]) -> None:
                published.append(("agenda", len(agenda["chapters"])))

            def set_overlay(self, **state: Any) -> None:
                published.append((state.get("chapter", 0), state.get("task", 0), state["status"]))

        storyboard.run(board, Recorder())
        expected = [("agenda", 2), (1, 1, "running"), (1, 2, "running"), (2, 0, "running"), (0, 0, "done")]
        if published != expected:
            failures.append(f"timeline was not published in run order: {published}")

        wide = storyboard.parse(
            {"steps": [{"beat": {"seconds": 0, "chapter": f"c{i}"}} for i in range(9)]}
        )
        if wide.agenda()["show_tasks"]:
            failures.append("an agenda card with nine chapters should drop its task lists")

    return failures


def check_setup() -> list[str]:
    """Setup runs before the camera rolls, so it must not be numbered or narrated like a take."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as raw_dir:
        directory = Path(raw_dir)
        (directory / "child.yaml").write_text(
            "setup:\n  - run_in_guest: {command: 'install-a.ps1', visible: false}\n"
            "steps:\n  - beat: 0\n",
            encoding="utf-8",
        )
        root = directory / "root.yaml"
        root.write_text(
            "setup:\n  - run_in_guest: {command: 'install-b.ps1', visible: false}\n"
            "steps:\n  - include: child\n  - beat: {seconds: 0, chapter: Deploy}\n",
            encoding="utf-8",
        )

        board = storyboard.load(root, lambda name: directory / f"{name}.yaml")
        commands = [s.args["command"] for s in board.setup_steps]
        if commands != ["install-a.ps1", "install-b.ps1"]:
            failures.append(f"an include did not contribute its setup first: {commands}")
        if [s.index for s in board.setup_steps] != [1, 2]:
            failures.append("setup steps were not numbered independently of the take")
        if len(board.steps) != 2:
            failures.append("setup steps leaked into the recorded steps")

        ran: list[str] = []

        class Recorder:
            def publish_agenda(self, agenda: dict[str, Any]) -> None:
                ran.append("agenda")

            def set_overlay(self, **state: Any) -> None:
                ran.append("overlay")

            def run_hidden(self, command: str, timeout: float) -> dict[str, Any]:
                ran.append(command)
                return {"exit_code": 0}

        report = storyboard.run(board, Recorder(), phase="setup")
        if ran != ["install-a.ps1", "install-b.ps1"]:
            failures.append(f"the setup phase did not run off camera in order: {ran}")
        if report["phase"] != "setup" or not report["completed"]:
            failures.append(f"the setup report was not usable: {report}")

        for label, text in {
            "narrated setup": "setup:\n  - beat: {seconds: 0, say: hello}\nsteps:\n  - beat: 0\n",
            "included setup": "setup:\n  - include: child\nsteps:\n  - beat: 0\n",
            "setup mapping": "setup: {run_in_guest: x}\nsteps:\n  - beat: 0\n",
        }.items():
            try:
                storyboard.parse(__import__("yaml").safe_load(text))
            except storyboard.StoryboardError:
                pass
            else:
                failures.append(f"accepted invalid setup: {label}")

        try:
            storyboard.run(board, Recorder(), phase="rehearsal")
        except storyboard.StoryboardError:
            pass
        else:
            failures.append("accepted an unknown phase")

    return failures


def main() -> int:
    failures = check_companion_values() + check_agenda() + check_setup()

    settings = Settings.from_env()
    found = settings.list_storyboards()
    print("storyboard dir:", settings.storyboard_dir)
    if not found:
        print("FAIL: no storyboards found")
        return 1

    for path in found:
        board = storyboard.load(path, settings.resolve_storyboard)
        chapters = board.chapters()
        print(
            f"OK  {board.name}: {len(board.steps)} steps, {len(board.setup_steps)} setup, "
            f"{len(chapters)} chapters, implicit beat {board.beat_between}s"
        )
        for chapter in chapters:
            tasks = ", ".join(t["title"] for t in chapter["tasks"]) or "-"
            print(f"    ch{chapter['index']} {chapter['title']} [{tasks}]")
        for step in board.setup_steps:
            print(f"    setup {step.index} {step.action} {step.args}")
        for step in board.steps:
            print(f"    {step.index:>2} {step.action} {step.args}")

    resolved = settings.resolve_storyboard("example-notepadpp")
    print("resolved by name ->", resolved.name)

    for label, text in BAD.items():
        try:
            storyboard.parse(__import__("yaml").safe_load(text))
        except storyboard.StoryboardError as exc:
            print(f"OK  rejected {label}: {exc}")
        else:
            print(f"FAIL: {label} should have been rejected")
            failures.append(f"accepted invalid storyboard: {label}")

    try:
        settings.resolve_storyboard("../../../etc/passwd")
    except FileNotFoundError as exc:
        print("OK  rejected traversal:", exc)
    else:
        print("FAIL: path traversal was not rejected")
        failures.append("accepted storyboard path traversal")

    print("PASS" if not failures else f"FAIL ({len(failures)}): {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
