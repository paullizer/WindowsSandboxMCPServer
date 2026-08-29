"""Phase 7 acceptance check: narration synthesis, caching, and measured cue durations.

    .venv\\Scripts\\python.exe tests\\check_speech.py

Host-side only - no sandbox, no recording. The offline half (SSML shape, secret guard, cue
extraction, duration wiring) always runs. The live half calls Azure Speech and is skipped with a
clear message when no resource is configured, so this stays useful on a machine without credentials.

Set AZURE_SPEECH_RESOURCE_ID and AZURE_SPEECH_REGION to exercise the live half.
"""

from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sandbox_mcp import speech, storyboard, transcript  # noqa: E402
from sandbox_mcp.config import Settings  # noqa: E402

STORYBOARD = """\
name: check-speech
steps:
  - beat:
      seconds: 0.05
      chapter: Opening
      say: Here is the first line of narration.
  - beat:
      seconds: 0.05
      say: A second line, on a different step.
      say_after: And a closing thought once the beat is done.
"""


def check_ssml() -> list[str]:
    failures: list[str] = []

    plain = speech.build_ssml("Hello & goodbye", "en-US-ChristopherNeural")
    if "Hello &amp; goodbye" not in plain:
        failures.append("SSML did not escape an ampersand")
    if 'xml:lang="en-US"' not in plain:
        failures.append(f"locale not derived from the voice name: {plain}")

    styled = speech.build_ssml("hi", "en-US-ChristopherNeural", style="narration-professional")
    if 'mstts:express-as style="narration-professional"' not in styled:
        failures.append("style was not applied")

    fitted = speech.build_ssml("hi", "en-US-ChristopherNeural", target_duration=5.34)
    if '<mstts:audioduration value="5340ms"/>' not in fitted:
        failures.append(f"target duration not emitted as ms: {fitted}")

    # An HD voice silently ignores audioduration, so asking for a refit has to fail loudly.
    try:
        speech.build_ssml("hi", "en-US-Christopher:DragonHDOmniLatestNeural", target_duration=3.0)
        failures.append("HD voice accepted a target duration instead of refusing")
    except speech.SpeechError:
        pass

    if speech.supports_audio_duration("en-US-Christopher:DragonHDOmniLatestNeural"):
        failures.append("HD voice reported as supporting audioduration")
    if not speech.supports_audio_duration("en-US-ChristopherNeural"):
        failures.append("standard neural voice reported as not supporting audioduration")

    injected = speech.build_ssml('say "<hi>"', "en-US-ChristopherNeural")
    if "<hi>" in injected.replace("&lt;hi&gt;", ""):
        failures.append("angle brackets in narration reached the SSML unescaped")
    return failures


def check_cues_and_guard(directory: Path) -> list[str]:
    failures: list[str] = []
    source = directory / "check-speech.yaml"
    source.write_text(STORYBOARD, encoding="utf-8")
    board = storyboard.load(source)

    cues = speech.cues_from_storyboard(board)
    ids = [c["id"] for c in cues]
    if ids != ["s1-say", "s2-say", "s2-say-after"]:
        failures.append(f"unexpected cue ids: {ids}")
    if cues[0]["chapter"] != "Opening":
        failures.append("chapter label not carried onto the cue")

    leaky = [{"id": "s1-say", "text": "the client_secret=hunter2 goes here"}]
    try:
        speech.synthesize_cues(Settings.from_env(), leaky, directory / "leak")
        failures.append("a cue containing a credential was accepted")
    except speech.SpeechError as exc:
        if "credential assignment" not in str(exc):
            failures.append(f"secret guard fired with the wrong reason: {exc}")
    return failures


def check_duration_wiring() -> list[str]:
    failures: list[str] = []
    text = "one two three four five six seven eight nine ten eleven twelve"
    estimated = transcript.cue_duration(text)
    measured = transcript.cue_duration(text, 9.5)
    if abs(measured - 9.5) > 0.001:
        failures.append(f"measured duration not used verbatim: {measured}")
    if abs(estimated - measured) < 0.5:
        failures.append("measured duration matched the estimate, so the test proves nothing")
    if transcript.cue_duration("hi", 0.4) != transcript.MIN_CUE_SECONDS:
        failures.append("minimum cue length not enforced for a very short measured line")
    return failures


def check_run_uses_durations(directory: Path) -> list[str]:
    """The runner has to look the duration up by cue id, or none of this reaches the subtitles."""
    failures: list[str] = []
    source = directory / "check-speech.yaml"
    board = storyboard.load(source)

    class Driver:
        def publish_agenda(self, *_: object, **__: object) -> None: ...
        def set_overlay(self, *_: object, **__: object) -> None: ...

    report = storyboard.run(board, Driver(), durations={"s1-say": 11.0})
    cues = {c["id"]: c for c in report["transcript"]}
    if "s1-say" not in cues:
        failures.append(f"cue ids missing from the report: {list(cues)}")
        return failures
    held = cues["s1-say"]["end"] - cues["s1-say"]["start"]
    if abs(held - 11.0) > 0.05:
        failures.append(f"runner ignored the measured duration: held {held}s")
    if abs((cues["s2-say"]["end"] - cues["s2-say"]["start"]) - 11.0) < 0.05:
        failures.append("an unmeasured cue picked up the measured duration")
    return failures


def check_live(directory: Path) -> list[str]:
    settings = Settings.from_env()
    status = speech.narration_status(settings)
    if not status["ready"]:
        print(f"SKIP live synthesis: {'; '.join(status['blockers'])}")
        return []

    failures: list[str] = []
    board = storyboard.load(directory / "check-speech.yaml")
    cues = speech.cues_from_storyboard(board)
    out = directory / "narration"

    first = speech.synthesize_cues(settings, cues, out)
    if first["cues"] != 3:
        failures.append(f"expected 3 cues, synthesized {first['cues']}")

    durations = speech.load_durations(out / speech.NARRATION_FILE)
    if len(durations) != 3:
        failures.append(f"narration.json did not round-trip 3 durations: {durations}")

    # The service reports the duration; the WAV is the ground truth. They have to agree.
    for cue_id, reported in durations.items():
        path = out / "cues" / f"{cue_id}.wav"
        if not path.is_file():
            failures.append(f"missing audio for {cue_id}")
            continue
        with wave.open(str(path), "rb") as handle:
            actual = handle.getnframes() / handle.getframerate()
        if abs(actual - reported) > 0.1:
            failures.append(
                f"{cue_id}: reported {reported:.3f}s but the WAV is {actual:.3f}s"
            )
        if reported <= 0:
            failures.append(f"{cue_id}: non-positive duration")

    second = speech.synthesize_cues(settings, cues, out)
    if second["from_cache"] != len(cues):
        failures.append(
            f"second pass re-synthesized {len(cues) - second['from_cache']} cue(s); cache is not working"
        )

    print(f"  live: {first['cues']} cues, {first['total_seconds']}s total, voice {first['voice']}")
    return failures


def main() -> int:
    failures: list[str] = []
    failures += check_ssml()
    failures += check_duration_wiring()
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        failures += check_cues_and_guard(directory)
        failures += check_run_uses_durations(directory)
        failures += check_live(directory)

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("OK: narration checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
