"""Declarative beat runner, so a take is repeatable and its pacing is tunable without code.

A storyboard is a list of single-key steps. Every step gets an implicit trailing beat, because
instant automation is unwatchable.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import yaml

from . import transcript

DEFAULT_BEAT = 0.6
VALUES_FILE = "values.json"
VALUES_EXAMPLE_FILE = "values.example.json"

_VALUE_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")

# Scalar shorthand: `send_keys: win` means `send_keys: {chord: win}`.
SHORTHAND: dict[str, str | None] = {
    "await_desktop": "timeout",
    "await_guest": "marker",
    "await_operator": "message",
    "answer_guest": None,
    "include": "storyboard",
    "beat": "seconds",
    "focus_sandbox": None,
    "send_keys": "chord",
    "type_text": "text",
    "paste_text": "text",
    "run_in_guest": "command",
    "scroll": "notches",
    "click": None,
    "move_mouse": None,
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "await_desktop": (),
    "await_guest": ("marker",),
    "await_operator": ("message",),
    "answer_guest": ("expect",),
    "include": ("storyboard",),
    "beat": ("seconds",),
    "focus_sandbox": (),
    "send_keys": ("chord",),
    "type_text": ("text",),
    "paste_text": ("text",),
    "run_in_guest": ("command",),
    "scroll": ("notches",),
    "click": ("x", "y"),
    "move_mouse": ("x", "y"),
}

ALLOWED: dict[str, tuple[str, ...]] = {
    "await_desktop": ("timeout",),
    "await_guest": ("marker", "timeout"),
    "await_operator": ("message", "log_tail"),
    "answer_guest": ("expect", "send", "keys", "submit", "timeout", "lines", "paste", "active_rows", "cps"),
    "include": ("storyboard",),
    "beat": ("seconds", "reason"),
    "focus_sandbox": (),
    "send_keys": ("chord",),
    "type_text": ("text", "cps", "allow_secret"),
    "paste_text": ("text", "restore_clipboard", "allow_secret"),
    "run_in_guest": ("command", "visible", "timeout", "await_exit"),
    "scroll": ("notches",),
    "click": ("x", "y", "button", "double", "move_duration_ms"),
    "move_mouse": ("x", "y", "duration_ms"),
}


class StoryboardError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    index: int
    action: str
    args: dict[str, Any]
    say: str = ""
    say_after: str = ""


@dataclass(frozen=True)
class Storyboard:
    name: str
    description: str
    beat_between: float
    steps: tuple[Step, ...]
    source: Path | None
    required_values: tuple[str, ...]
    values_source: Path | None
    using_example_values: bool

    @property
    def configured(self) -> bool:
        return not self.required_values or not self.using_example_values

    def require_configured(self) -> None:
        if not self.configured:
            raise StoryboardError(
                f"{self.name} is using {VALUES_EXAMPLE_FILE}; copy it to {VALUES_FILE}, "
                "fill in your values, and replay the storyboard"
            )

    def outline(self) -> list[dict[str, Any]]:
        return [
            {
                "step": s.index,
                "action": s.action,
                "args": s.args,
                **({"say": s.say} if s.say else {}),
                **({"say_after": s.say_after} if s.say_after else {}),
            }
            for s in self.steps
        ]


def _normalise(action: str, raw: Any, index: int) -> dict[str, Any]:
    if raw is None:
        args: dict[str, Any] = {}
    elif isinstance(raw, dict):
        args = dict(raw)
    else:
        key = SHORTHAND.get(action)
        if key is None:
            raise StoryboardError(
                f"step {index} ({action}): needs a mapping of arguments, got {raw!r}"
            )
        args = {key: raw}

    unknown = set(args) - set(ALLOWED[action])
    if unknown:
        raise StoryboardError(
            f"step {index} ({action}): unknown argument(s) {sorted(unknown)}; "
            f"allowed: {list(ALLOWED[action]) or 'none'}"
        )
    missing = [key for key in REQUIRED[action] if key not in args]
    if missing:
        raise StoryboardError(f"step {index} ({action}): missing required argument(s) {missing}")
    return args


def parse(
    document: Any,
    source: Path | None = None,
    resolve: Callable[[str], Path] | None = None,
    seen: frozenset[str] = frozenset(),
    *,
    values: dict[str, Any] | None = None,
    values_source: Path | None = None,
    using_example_values: bool = False,
    required_values: frozenset[str] = frozenset(),
) -> Storyboard:
    if not isinstance(document, dict):
        raise StoryboardError("a storyboard must be a mapping with a 'steps' list")
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise StoryboardError("a storyboard must contain a non-empty 'steps' list")

    defaults = document.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise StoryboardError("'defaults' must be a mapping")

    steps: list[Step] = []
    all_required_values = set(required_values)
    position = 0
    for entry_number, entry in enumerate(raw_steps, start=1):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise StoryboardError(
                f"step {entry_number}: each step is a single-key mapping such as "
                f"'- send_keys: ctrl+l', got {entry!r}"
            )
        action, raw = next(iter(entry.items()))
        if action not in ALLOWED:
            raise StoryboardError(
                f"step {entry_number}: unknown action {action!r}; known actions: {sorted(ALLOWED)}"
            )

        if action == "include":
            # Expanded here rather than at run time so dry_run, from_step and to_step all see one
            # flat list of steps.
            name = str(_normalise(action, raw, entry_number)["storyboard"])
            if resolve is None:
                raise StoryboardError(
                    f"step {entry_number}: include needs a storyboard directory to resolve {name!r}"
                )
            try:
                child_path = resolve(name)
            except FileNotFoundError as exc:
                raise StoryboardError(f"step {entry_number}: {exc}") from exc
            if str(child_path) in seen:
                raise StoryboardError(f"step {entry_number}: circular include of {name!r}")
            child = load(
                child_path,
                resolve,
                seen | {str(child_path)},
                values=values,
                values_source=values_source,
                using_example_values=using_example_values,
            )
            all_required_values.update(child.required_values)
            for child_step in child.steps:
                position += 1
                steps.append(replace(child_step, index=position))
            continue

        # Narration is valid on every action, so strip it before the per-action argument check.
        say = say_after = ""
        if isinstance(raw, dict):
            raw = dict(raw)
            say = str(raw.pop("say", "") or "").strip()
            say_after = str(raw.pop("say_after", "") or "").strip()
        position += 1
        steps.append(
            Step(
                index=position,
                action=action,
                args=_normalise(action, raw, entry_number),
                say=say,
                say_after=say_after,
            )
        )

    return Storyboard(
        name=str(document.get("name") or (source.stem if source else "storyboard")),
        description=str(document.get("description") or ""),
        beat_between=float(defaults.get("beat", DEFAULT_BEAT)),
        steps=tuple(steps),
        source=source,
        required_values=tuple(sorted(all_required_values)),
        values_source=values_source,
        using_example_values=using_example_values,
    )


def _read_values(directory: Path) -> tuple[dict[str, Any], Path | None, bool]:
    actual = directory / VALUES_FILE
    example = directory / VALUES_EXAMPLE_FILE
    source = actual if actual.is_file() else example if example.is_file() else None
    if source is None:
        return {}, None, False
    try:
        values = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoryboardError(f"{source.name} is not valid JSON: {exc}") from exc
    if not isinstance(values, dict):
        raise StoryboardError(f"{source.name} must contain a JSON object")
    return values, source, source == example


def _lookup_value(
    name: str, values: dict[str, Any], directory: Path, source: Path | None
) -> Any:
    current: Any = values
    for part in name.split("."):
        if not isinstance(current, dict) or part not in current:
            if source is None:
                hint = f"copy {VALUES_EXAMPLE_FILE} to {VALUES_FILE} and fill it in"
            else:
                hint = f"add it to {source.name}"
            raise StoryboardError(
                f"missing storyboard value {name!r}; {hint} in {directory}"
            )
        current = current[part]
    if isinstance(current, (dict, list)):
        raise StoryboardError(f"storyboard value {name!r} must be a JSON scalar")
    return current


def _stringify_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return "null"
    return str(value)


def _resolve_values(
    value: Any,
    values: dict[str, Any],
    directory: Path,
    source: Path | None,
    required: set[str],
) -> Any:
    if isinstance(value, list):
        return [_resolve_values(item, values, directory, source, required) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_values(item, values, directory, source, required)
            for key, item in value.items()
        }
    if not isinstance(value, str):
        return value

    matches = list(_VALUE_TOKEN.finditer(value))
    if not matches:
        return value

    resolved: dict[str, Any] = {}
    for match in matches:
        name = match.group(1)
        required.add(name)
        resolved[name] = _lookup_value(name, values, directory, source)

    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return resolved[matches[0].group(1)]
    return _VALUE_TOKEN.sub(
        lambda match: _stringify_value(resolved[match.group(1)]), value
    )


def load(
    path: Path,
    resolve: Callable[[str], Path] | None = None,
    seen: frozenset[str] = frozenset(),
    *,
    values: dict[str, Any] | None = None,
    values_source: Path | None = None,
    using_example_values: bool = False,
) -> Storyboard:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise StoryboardError(f"{path.name} is not valid YAML: {exc}") from exc
    if values is None:
        values, values_source, using_example_values = _read_values(path.parent)
    required_values: set[str] = set()
    document = _resolve_values(
        document, values, path.parent, values_source, required_values
    )
    return parse(
        document,
        path,
        resolve,
        seen | {str(path)},
        values=values,
        values_source=values_source,
        using_example_values=using_example_values,
        required_values=frozenset(required_values),
    )


def _beat(driver: Any, args: dict[str, Any]) -> dict[str, Any]:
    seconds = max(0.0, float(args["seconds"]))
    time.sleep(seconds)
    return {"waited_seconds": seconds, "reason": args.get("reason", "")}


ACTIONS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "await_desktop": lambda d, a: d.await_desktop(float(a.get("timeout", 120))),
    "await_guest": lambda d, a: d.channel().wait_for_marker(
        str(a["marker"]), float(a.get("timeout", 300))
    ),
    "answer_guest": lambda d, a: d.answer_guest(
        str(a["expect"]),
        _stringify_value(a.get("send", "")),
        tuple(a.get("keys") or ()),
        bool(a.get("submit", True)),
        float(a.get("timeout", 300)),
        int(a.get("lines", 25)),
        paste=bool(a.get("paste", False)),
        active_rows=int(a.get("active_rows", 8)),
        cps=float(a.get("cps", 14)),
    ),
    "beat": _beat,
    "focus_sandbox": lambda d, a: d.focus(),
    "send_keys": lambda d, a: d.send_keys(str(a["chord"])),
    "type_text": lambda d, a: d.type_text(
        str(a["text"]), float(a.get("cps", 14)), bool(a.get("allow_secret", False))
    ),
    "paste_text": lambda d, a: d.paste_text(
        str(a["text"]), bool(a.get("restore_clipboard", True)), bool(a.get("allow_secret", False))
    ),
    "run_in_guest": lambda d, a: (
        d.run_visible(str(a["command"]), bool(a.get("await_exit", True)), float(a.get("timeout", 600)))
        if bool(a.get("visible", True))
        else d.run_hidden(str(a["command"]), float(a.get("timeout", 600)))
    ),
    "scroll": lambda d, a: d.scroll(int(a["notches"])),
    "click": lambda d, a: d.click(
        int(a["x"]),
        int(a["y"]),
        str(a.get("button", "left")),
        bool(a.get("double", False)),
        int(a.get("move_duration_ms", 250)),
    ),
    "move_mouse": lambda d, a: d.move_mouse(
        int(a["x"]), int(a["y"]), int(a.get("duration_ms", 250))
    ),
}

# Detail fields worth keeping in the report; transcripts and window dumps are too big.
_INTERESTING = (
    "exit_code", "succeeded", "ok", "input_clean", "delivered", "stdout_tail", "stderr_tail",
    "waited_seconds", "reason", "name", "detail", "characters", "key", "modifiers",
    "input_focus", "phase", "prompt_index",
)


def _summarise(detail: Any) -> Any:
    if not isinstance(detail, dict):
        return detail
    kept = {k: v for k, v in detail.items() if k in _INTERESTING}
    return kept or {"ok": True}


def run(storyboard: Storyboard, driver: Any, from_step: int = 1, to_step: int = 0) -> dict[str, Any]:
    """Replay every beat in order, stopping at the first failure and saying which step broke.

    An await_operator step suspends instead of blocking, so a human can do something out of band
    (sign in, approve a prompt) and the caller resumes with from_step. to_step stops early on
    purpose, which is how a caller runs a storyboard in segments - to pause a recording between
    them, for instance.
    """
    storyboard.require_configured()

    results: list[dict[str, Any]] = []
    cues: list[dict[str, Any]] = []
    started = time.monotonic()
    last = to_step if to_step > 0 else len(storyboard.steps)
    pending = [s for s in storyboard.steps if from_step <= s.index <= last]

    def add_cue(step: Step, text: str, at: float) -> None:
        cues.append(
            {
                "step": step.index,
                "start": round(at, 2),
                "end": round(at + transcript.cue_duration(text), 2),
                "text": text,
            }
        )

    for step in pending:
        if step.action == "await_operator":
            if step.say:
                add_cue(step, step.say, time.monotonic() - started)
            return {
                "storyboard": storyboard.name,
                "completed": False,
                "suspended": True,
                "at_step": step.index,
                "message": str(step.args["message"]),
                "guest_log_tail": _guest_tail(driver, int(step.args.get("log_tail", 25))),
                "resume_with": {"name": storyboard.name, "from_step": step.index + 1},
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "steps": results,
                "transcript": cues,
            }

        step_started = time.monotonic()
        if step.say:
            add_cue(step, step.say, step_started - started)
        try:
            detail = ACTIONS[step.action](driver, step.args)
        except Exception as exc:  # noqa: BLE001 - the report is the error channel
            results.append(
                {
                    "step": step.index,
                    "action": step.action,
                    "args": step.args,
                    "seconds": round(time.monotonic() - step_started, 2),
                    "ok": False,
                    "error": str(exc),
                }
            )
            return {
                "storyboard": storyboard.name,
                "completed": False,
                "failed_step": step.index,
                "failed_action": step.action,
                "error": str(exc),
                "resume_with": {"name": storyboard.name, "from_step": step.index},
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "steps": results,
                "transcript": cues,
            }

        if step.say_after:
            add_cue(step, step.say_after, time.monotonic() - started)

        results.append(
            {
                "step": step.index,
                "action": step.action,
                "args": step.args,
                "at_seconds": round(step_started - started, 2),
                "seconds": round(time.monotonic() - step_started, 2),
                "ok": True,
                "detail": _summarise(detail),
            }
        )
        # An explicit beat already holds the shot; adding another would double it.
        if step.action != "beat" and storyboard.beat_between > 0:
            time.sleep(storyboard.beat_between)

    finished = last >= len(storyboard.steps)
    return {
        "storyboard": storyboard.name,
        "completed": finished,
        "stopped_early": not finished,
        "steps_run": len(results),
        "resume_with": None if finished else {"name": storyboard.name, "from_step": last + 1},
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "steps": results,
        "transcript": cues,
    }


def _guest_tail(driver: Any, lines: int) -> list[str]:
    """What the operator needs to see - typically a device code printed by a command that has not
    returned yet, which the transcript cannot show."""
    try:
        return driver.read_guest_screen(lines)["lines"]
    except Exception:  # noqa: BLE001 - fall back rather than fail the pause
        pass
    try:
        return driver.channel().transcript_tail(lines)
    except Exception:  # noqa: BLE001 - the pause must not fail because the guest is quiet
        return []
