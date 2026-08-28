"""Prompter build check. Host-only: reads storyboards and writes an HTML file, touches no sandbox."""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sandbox_mcp.config import Settings  # noqa: E402
from sandbox_mcp.prompter import build, collect, render  # noqa: E402


def main() -> int:
    settings = Settings.from_env()
    failures: list[str] = []

    boards = collect(settings)
    if not boards:
        print("FAIL no storyboards with narration were found")
        return 1
    for board in boards:
        cues = board["cues"]
        print(f"OK  {board['name']}: {len(cues)} cues, ~{board['estimate']}s of speech")
        if any(not c["text"].strip() for c in cues):
            failures.append(f"{board['name']}: empty cue")
        if any(c["estimate"] <= 0 for c in cues):
            failures.append(f"{board['name']}: non-positive estimate")

    out = Path(tempfile.mkdtemp()) / "prompter.html"
    result = build(out, settings)
    text = out.read_text(encoding="utf-8")

    if "/*__DATA__*/null" in text:
        failures.append("data placeholder was not replaced")
    if "</" in json.dumps(boards) and "<\\/" not in text:
        failures.append("'</' in narration was not escaped for the script block")
    for marker in ("http://", "https://cdn", "src="):
        if marker in text.split("<script>")[0] and marker == "src=":
            failures.append("template pulls an external resource")
    embedded = re.search(r"const BOARDS = (\[.*?\]);\n", text, re.S)
    if not embedded:
        failures.append("embedded board data is not parseable")
    else:
        parsed = json.loads(embedded.group(1).replace("<\\/", "</"))
        if len(parsed) != len(boards):
            failures.append(f"embedded {len(parsed)} boards, expected {len(boards)}")

    print(f"OK  wrote {result['path']} ({result['cues']} cues)")
    print("PASS" if not failures else f"FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
