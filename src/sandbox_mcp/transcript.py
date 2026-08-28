"""Turns storyboard narration into a timed transcript.

Cues are timed from the actual run rather than estimated up front, because a step that takes eight
minutes on one machine takes twelve on another. Narration is authored next to the step it
describes; only the clock comes from the recording.
"""

from __future__ import annotations

from typing import Any, Iterable

# Rough conversational pace, used to decide how long a cue stays on screen.
WORDS_PER_SECOND = 2.6
MIN_CUE_SECONDS = 2.5


def cue_duration(text: str) -> float:
    words = max(1, len(text.split()))
    return max(MIN_CUE_SECONDS, words / WORDS_PER_SECOND)


def _timestamp(seconds: float, comma: bool = False) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    if millis == 1000:
        whole, millis = whole + 1, 0
    separator = "," if comma else "."
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d}{separator}{millis:03d}"


def _shift(cues: Iterable[dict[str, Any]], offset: float) -> list[dict[str, Any]]:
    moved = [
        {**cue, "start": max(0.0, cue["start"] + offset), "end": max(0.0, cue["end"] + offset)}
        for cue in cues
    ]
    # Never let one cue run into the next; overlapping subtitles are unreadable.
    for current, following in zip(moved, moved[1:]):
        current["end"] = min(current["end"], following["start"] - 0.05)
    return [c for c in moved if c["end"] > c["start"]]


def to_vtt(cues: Iterable[dict[str, Any]], offset: float = 0.0) -> str:
    lines = ["WEBVTT", ""]
    for index, cue in enumerate(_shift(cues, offset), start=1):
        lines.append(str(index))
        lines.append(f"{_timestamp(cue['start'])} --> {_timestamp(cue['end'])}")
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines)


def to_srt(cues: Iterable[dict[str, Any]], offset: float = 0.0) -> str:
    lines: list[str] = []
    for index, cue in enumerate(_shift(cues, offset), start=1):
        lines.append(str(index))
        lines.append(
            f"{_timestamp(cue['start'], comma=True)} --> {_timestamp(cue['end'], comma=True)}"
        )
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines)


def to_script(cues: Iterable[dict[str, Any]], name: str, offset: float = 0.0) -> str:
    """A readable voiceover script - timecodes plus what to say, for recording narration."""
    lines = [f"# {name} - narration", ""]
    for cue in _shift(cues, offset):
        lines.append(f"**{_timestamp(cue['start'])}**  _(step {cue['step']})_")
        lines.append("")
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines)
