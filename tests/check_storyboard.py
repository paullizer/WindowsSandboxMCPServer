"""Storyboard parser checks. Pure host-side, no sandbox required.

    .venv\\Scripts\\python.exe tests\\check_storyboard.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

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


def main() -> int:
    failures = check_companion_values()

    settings = Settings.from_env()
    found = settings.list_storyboards()
    print("storyboard dir:", settings.storyboard_dir)
    if not found:
        print("FAIL: no storyboards found")
        return 1

    for path in found:
        board = storyboard.load(path, settings.resolve_storyboard)
        print(f"OK  {board.name}: {len(board.steps)} steps, implicit beat {board.beat_between}s")
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
