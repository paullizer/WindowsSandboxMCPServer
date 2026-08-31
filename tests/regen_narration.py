r"""Rewrite narration.json for one or more storyboards without re-calling Azure Speech.

    .venv\Scripts\python.exe tests\regen_narration.py prereqs-from-clean-windows ...

Every cue is already in the content-addressed cache, so this only re-emits the document. Use it
after changing the narration schema, when the long-running MCP server still holds the old module.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sandbox_mcp import speech  # noqa: E402
from sandbox_mcp.config import Settings  # noqa: E402
from sandbox_mcp.storyboard import load as load_storyboard  # noqa: E402


def main(names: list[str]) -> int:
    settings = Settings.from_env()
    for name in names:
        board = load_storyboard(settings.resolve_storyboard(name), settings.resolve_storyboard)
        cues = speech.cues_from_storyboard(board, "steps")
        out_dir = settings.ensure_work_dir() / "narration" / board.name
        result = speech.synthesize_cues(settings, cues, out_dir)
        print(
            f"{board.name}: {result['cues']} cues, {result['from_cache']} from cache -> "
            f"{result['narration']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
