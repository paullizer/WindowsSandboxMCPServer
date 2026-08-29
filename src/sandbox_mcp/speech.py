"""Speaks storyboard narration with Azure Speech, and records how long each line actually took.

The words are authored next to the step they describe; this module only turns them into audio. The
durations it measures matter as much as the audio: `transcript.py` guesses cue length from a
words-per-second constant, and the editor needs to know how much room a line really needs before it
can decide how much dead air to cut. A measured duration replaces both guesses.

Synthesis is host-side only and never runs during a take. Nothing here may reach the guest.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import wave
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape, quoteattr

from .config import Settings
from .wininput import detect_secret

SCOPE = "https://cognitiveservices.azure.com/.default"
_SSML_NS = "http://www.w3.org/2001/10/synthesis"
_MSTTS_NS = "http://www.w3.org/2001/mstts"

NARRATION_SCHEMA = "sandbox-narration/1"
NARRATION_FILE = "narration.json"


class SpeechError(RuntimeError):
    pass


def _sdk() -> Any:
    try:
        import azure.cognitiveservices.speech as speechsdk
    except ImportError as exc:  # noqa: PERF203 - the hint is the whole point of catching this
        raise SpeechError(
            "The Azure Speech SDK is not installed. Install the optional extra with "
            "`pip install -e .[speech]` (or `pip install azure-cognitiveservices-speech "
            "azure-identity`) and try again."
        ) from exc
    return speechsdk


def _credential() -> Any:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise SpeechError(
            "azure-identity is not installed. Install the optional extra with "
            "`pip install -e .[speech]`."
        ) from exc
    return DefaultAzureCredential()


def locale_of(voice: str) -> str:
    """`en-US-ChristopherNeural` and `en-US-Christopher:DragonHDOmniLatestNeural` are both en-US."""
    parts = voice.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else "en-US"


def supports_audio_duration(voice: str) -> bool:
    """HD voices carry a `:basemodel` suffix and silently ignore mstts:audioduration."""
    return ":" not in voice


def build_ssml(
    text: str,
    voice: str,
    style: str = "",
    rate: str = "",
    target_duration: float = 0.0,
) -> str:
    """Wrap a line of narration in SSML, honouring only the tags the chosen voice supports."""
    body = escape(text.strip())
    if rate:
        body = f'<prosody rate={quoteattr(rate)}>{body}</prosody>'
    if style:
        body = f'<mstts:express-as style={quoteattr(style)}>{body}</mstts:express-as>'
    duration = ""
    if target_duration > 0:
        if not supports_audio_duration(voice):
            raise SpeechError(
                f"Voice {voice!r} is an HD voice and ignores mstts:audioduration, so it cannot be "
                "refitted to a target duration. Use a standard neural voice for refit passes."
            )
        duration = f'<mstts:audioduration value="{int(round(target_duration * 1000))}ms"/>'
    return (
        f'<speak version="1.0" xmlns="{_SSML_NS}" xmlns:mstts="{_MSTTS_NS}" '
        f'xml:lang="{locale_of(voice)}">'
        f"<voice name={quoteattr(voice)}>{duration}{body}</voice>"
        "</speak>"
    )


def _speech_config(settings: Settings, speechsdk: Any) -> Any:
    target: dict[str, Any] = {}
    if settings.speech_endpoint:
        target["endpoint"] = settings.speech_endpoint
    elif settings.speech_region:
        target["region"] = settings.speech_region
    else:
        raise SpeechError(
            "Set AZURE_SPEECH_REGION (or AZURE_SPEECH_ENDPOINT) before synthesizing narration."
        )

    if settings.speech_resource_id:
        # Entra is the default path so no key has to exist on disk to leak into a mapped folder.
        token = _credential().get_token(SCOPE).token
        config = speechsdk.SpeechConfig(
            auth_token=f"aad#{settings.speech_resource_id}#{token}", **target
        )
    elif settings.speech_key:
        config = speechsdk.SpeechConfig(subscription=settings.speech_key, **target)
    else:
        raise SpeechError(
            "No Speech credentials. Set AZURE_SPEECH_RESOURCE_ID for Entra auth (preferred), or "
            "AZURE_SPEECH_KEY to fall back to a key."
        )
    config.set_speech_synthesis_output_format(_output_format(speechsdk, settings.speech_format))
    return config


def _output_format(speechsdk: Any, name: str) -> Any:
    wanted = name.replace("-", "").replace("_", "").lower()
    for member in speechsdk.SpeechSynthesisOutputFormat:
        if member.name.lower() == wanted:
            return member
    raise SpeechError(f"Unknown AZURE_SPEECH_FORMAT {name!r}")


def wav_duration(audio: bytes) -> float:
    """Length of a RIFF payload, read from its own header rather than trusted from the service."""
    with wave.open(io.BytesIO(audio), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return frames / rate if rate else 0.0


def _cache_key(text: str, voice: str, style: str, rate: str, target_duration: float, fmt: str) -> str:
    material = "\u0000".join(
        [text.strip(), voice, style, rate, f"{target_duration:.3f}", fmt]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def cues_from_storyboard(storyboard: Any, phase: str = "steps") -> list[dict[str, Any]]:
    """Every `say:` and `say_after:` in run order, with placeholders already resolved by load()."""
    cues: list[dict[str, Any]] = []
    for step in storyboard.phase_steps(phase):
        for kind, text in (("say", step.say), ("say_after", step.say_after)):
            if not text:
                continue
            cues.append(
                {
                    "id": f"s{step.index}-{kind.replace('_', '-')}",
                    "step": step.index,
                    "kind": kind,
                    "chapter": step.chapter,
                    "task": step.task,
                    "text": " ".join(text.split()),
                }
            )
    return cues


def _guard(cues: Iterable[dict[str, Any]]) -> None:
    """Narration interpolates {{value.*}}, so a careless placeholder could speak a secret aloud."""
    for cue in cues:
        label = detect_secret(cue["text"])
        if label is not None:
            raise SpeechError(
                f"Cue {cue['id']} looks like it contains a {label}. Narration ends up in a WAV and "
                "in the caption files, so it is refused rather than spoken."
            )


class Synthesizer:
    """One batch of synthesis. Short-lived on purpose: an Entra token expires in ~10 minutes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sdk = _sdk()
        self._config = _speech_config(settings, self._sdk)
        self._cache = settings.ensure_speech_cache_dir()

    def voices(self, locale: str = "") -> list[dict[str, Any]]:
        synthesizer = self._sdk.SpeechSynthesizer(speech_config=self._config, audio_config=None)
        result = synthesizer.get_voices_async(locale).get()
        if result.reason != self._sdk.ResultReason.VoicesListRetrieved:
            raise SpeechError(f"Could not list voices: {result.error_details}")
        listed = []
        for voice in result.voices:
            listed.append(
                {
                    "name": voice.short_name,
                    "display_name": voice.local_name,
                    "locale": voice.locale,
                    "gender": voice.gender.name,
                    "styles": list(voice.style_list or []),
                    "voice_type": voice.voice_type.name,
                    "supports_audio_duration": supports_audio_duration(voice.short_name),
                }
            )
        listed.sort(key=lambda v: (v["locale"], v["name"]))
        return listed

    def speak(
        self,
        text: str,
        voice: str,
        style: str = "",
        rate: str = "",
        target_duration: float = 0.0,
    ) -> dict[str, Any]:
        """Synthesize one line, from cache when nothing about it has changed."""
        fmt = self._settings.speech_format
        key = _cache_key(text, voice, style, rate, target_duration, fmt)
        cached = self._cache / f"{key}.wav"
        meta_path = self._cache / f"{key}.json"
        if cached.is_file() and meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return {**meta, "path": cached, "cached": True}

        ssml = build_ssml(text, voice, style, rate, target_duration)
        boundaries: list[dict[str, Any]] = []
        synthesizer = self._sdk.SpeechSynthesizer(speech_config=self._config, audio_config=None)
        synthesizer.synthesis_word_boundary.connect(
            lambda evt: boundaries.append(
                # audio_offset is in 100-nanosecond ticks.
                {"text": evt.text, "offset": round(evt.audio_offset / 10_000_000, 3)}
            )
        )
        result = synthesizer.speak_ssml_async(ssml).get()
        if result.reason != self._sdk.ResultReason.SynthesizingAudioCompleted:
            raise SpeechError(_failure(self._sdk, result))

        audio = bytes(result.audio_data)
        measured = wav_duration(audio)
        reported = result.audio_duration.total_seconds() if result.audio_duration else 0.0
        meta = {
            "duration": round(reported or measured, 3),
            "measured_duration": round(measured, 3),
            "bytes": len(audio),
            "words": boundaries,
        }
        cached.write_bytes(audio)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {**meta, "path": cached, "cached": False}


def _failure(speechsdk: Any, result: Any) -> str:
    if result.reason != speechsdk.ResultReason.Canceled:
        return f"Synthesis failed: {result.reason}"
    details = result.cancellation_details
    message = f"Synthesis cancelled: {details.reason}"
    if details.error_details:
        message = f"{message} - {details.error_details}"
    if details.reason == speechsdk.CancellationReason.Error and "401" in (
        details.error_details or ""
    ):
        message += (
            "\nA 401 with Entra auth usually means either the Speech resource has no custom "
            "subdomain, or the caller is missing the 'Cognitive Services Speech User' role."
        )
    return message


def synthesize_cues(
    settings: Settings,
    cues: list[dict[str, Any]],
    out_dir: Path,
    voice: str = "",
    style: str = "",
    rate: str = "",
    target_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Speak every cue, write the WAVs and narration.json, and report measured durations."""
    _guard(cues)
    voice = voice or settings.speech_voice
    style = style or settings.speech_style
    rate = rate or settings.speech_rate
    targets = target_durations or {}

    audio_dir = out_dir / "cues"
    audio_dir.mkdir(parents=True, exist_ok=True)
    synth = Synthesizer(settings)

    rendered: list[dict[str, Any]] = []
    reused = 0
    for cue in cues:
        spoken = synth.speak(cue["text"], voice, style, rate, float(targets.get(cue["id"], 0.0)))
        reused += 1 if spoken["cached"] else 0
        destination = audio_dir / f"{cue['id']}.wav"
        shutil.copyfile(spoken["path"], destination)
        rendered.append(
            {
                **{k: v for k, v in cue.items() if v != ""},
                "audio": f"cues/{destination.name}",
                "duration": spoken["duration"],
                "words": spoken["words"],
            }
        )

    document = {
        "schema": NARRATION_SCHEMA,
        "voice": voice,
        "style": style,
        "rate": rate,
        "format": settings.speech_format,
        "total_seconds": round(sum(c["duration"] for c in rendered), 2),
        "cues": rendered,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / NARRATION_FILE).write_text(json.dumps(document, indent=2), encoding="utf-8")
    return {
        "narration": str(out_dir / NARRATION_FILE),
        "audio_dir": str(audio_dir),
        "cues": len(rendered),
        "from_cache": reused,
        "voice": voice,
        "total_seconds": document["total_seconds"],
    }


def load_durations(path: Path) -> dict[str, float]:
    """Measured cue durations keyed by cue id, for transcripts to use instead of the estimate."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    cues = document.get("cues") if isinstance(document, dict) else None
    if not isinstance(cues, list):
        return {}
    return {
        str(cue["id"]): float(cue["duration"])
        for cue in cues
        if isinstance(cue, dict) and "id" in cue and "duration" in cue
    }


def narration_status(settings: Settings) -> dict[str, Any]:
    """Whether narration could be synthesized right now, without actually calling the service."""
    try:
        _sdk()
        installed = True
    except SpeechError:
        installed = False

    auth = (
        "entra"
        if settings.speech_resource_id
        else "key"
        if settings.speech_key
        else None
    )
    blockers: list[str] = []
    if not installed:
        blockers.append("Speech SDK not installed - `pip install -e .[speech]`")
    if auth is None:
        blockers.append("No credentials - set AZURE_SPEECH_RESOURCE_ID (preferred) or AZURE_SPEECH_KEY")
    if not settings.speech_region and not settings.speech_endpoint:
        blockers.append("Set AZURE_SPEECH_REGION or AZURE_SPEECH_ENDPOINT")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "sdk_installed": installed,
        "auth": auth,
        "resource_id": settings.speech_resource_id or None,
        "region": settings.speech_region or None,
        "endpoint": settings.speech_endpoint or None,
        "voice": settings.speech_voice,
        "supports_audio_duration": supports_audio_duration(settings.speech_voice),
        "format": settings.speech_format,
        "cache_dir": str(settings.speech_cache_dir) if settings.speech_cache_dir else None,
    }
