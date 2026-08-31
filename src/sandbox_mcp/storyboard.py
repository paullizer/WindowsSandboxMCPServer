"""Declarative beat runner, so a take is repeatable and its pacing is tunable without code.

A storyboard is a list of single-key steps. Every step gets an implicit trailing beat, because
instant automation is unwatchable. A `setup:` list holds the same kind of steps for work that has
to happen before the camera rolls.
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
# Matches the narration assembler's own spacing, so a gap here survives the edit.
CUE_GAP = 0.05
VALUES_FILE = "values.json"
VALUES_EXAMPLE_FILE = "values.example.json"

# `setup` runs before the camera rolls, `steps` is the take itself.
PHASES = ("setup", "steps")

# An agenda card is only worth showing if it can be read at a glance. Past either limit the
# chapter titles alone say more than a wall of sub-steps, so the card drops the tasks and the
# timeline bar keeps them for the chapter that is actually running.
AGENDA_CARD_MAX_CHAPTERS = 6
AGENDA_CARD_MAX_TASKS = 18

_VALUE_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*\}\}")

# Valid on every action, so they are stripped before the per-action argument check.
ANNOTATIONS = ("say", "say_after", "chapter", "task")

# Scalar shorthand: `send_keys: win` means `send_keys: {chord: win}`.
SHORTHAND: dict[str, str | None] = {
    "await_desktop": "timeout",
    "await_guest": "marker",
    "await_operator": "message",
    "answer_guest": None,
    "include": "storyboard",
    "beat": "seconds",
    "focus_sandbox": None,
    "show_agenda": "seconds",
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
    "show_agenda": (),
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
    "show_agenda": ("seconds",),
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
    chapter: str = ""
    task: str = ""


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
    agenda_title: str = ""
    agenda_subtitle: str = ""
    series: str = ""
    brand_logo: str = ""
    brand_intro: str = ""
    setup_steps: tuple[Step, ...] = ()

    @property
    def configured(self) -> bool:
        return not self.required_values or not self.using_example_values

    def require_configured(self) -> None:
        if not self.configured:
            raise StoryboardError(
                f"{self.name} is using {VALUES_EXAMPLE_FILE}; copy it to {VALUES_FILE}, "
                "fill in your values, and replay the storyboard"
            )

    def phase_steps(self, phase: str) -> tuple[Step, ...]:
        if phase not in PHASES:
            raise StoryboardError(f"unknown phase {phase!r}; expected one of {list(PHASES)}")
        return self.setup_steps if phase == "setup" else self.steps

    def outline(self, phase: str = "steps") -> list[dict[str, Any]]:
        return [
            {
                "step": s.index,
                "action": s.action,
                "args": s.args,
                **({"say": s.say} if s.say else {}),
                **({"say_after": s.say_after} if s.say_after else {}),
                **({"chapter": s.chapter} if s.chapter else {}),
                **({"task": s.task} if s.task else {}),
            }
            for s in self.phase_steps(phase)
        ]

    def chapters(self) -> list[dict[str, Any]]:
        """The agenda, in run order, with the span of steps each chapter and task covers.

        Built from the flattened step list, so an `include:` folds its chapters into the parent's
        agenda rather than starting a second one - which is the only way a run stitched together
        from several storyboards can show one honest agenda up front.
        """
        found: list[dict[str, Any]] = []
        for step in self.steps:
            if step.chapter and (not found or found[-1]["title"] != step.chapter):
                found.append(
                    {
                        "index": len(found) + 1,
                        "title": step.chapter,
                        "first_step": step.index,
                        "last_step": step.index,
                        "tasks": [],
                    }
                )
            if not found:
                continue  # Steps before the first chapter belong to no chapter.
            chapter = found[-1]
            chapter["last_step"] = step.index
            tasks = chapter["tasks"]
            if step.task and (not tasks or tasks[-1]["title"] != step.task):
                tasks.append(
                    {
                        "index": len(tasks) + 1,
                        "title": step.task,
                        "first_step": step.index,
                        "last_step": step.index,
                    }
                )
            elif tasks:
                tasks[-1]["last_step"] = step.index
        return found

    def agenda(self) -> dict[str, Any]:
        """What the on-camera agenda card shows before the run starts."""
        chapters = self.chapters()
        tasks = sum(len(c["tasks"]) for c in chapters)
        return {
            "title": self.agenda_title or self.name,
            "subtitle": self.agenda_subtitle,
            "series": self.series,
            **({"logo": self.brand_logo} if self.brand_logo else {}),
            **({"intro": self.brand_intro} if self.brand_intro else {}),
            "show_tasks": len(chapters) <= AGENDA_CARD_MAX_CHAPTERS and tasks <= AGENDA_CARD_MAX_TASKS,
            "chapters": [
                {
                    "index": c["index"],
                    "title": c["title"],
                    "tasks": [t["title"] for t in c["tasks"]],
                }
                for c in chapters
            ],
        }


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

def _parse_setup(document: Any, inherited: list[Step]) -> tuple[Step, ...]:
    """Steps that run before the recording starts, so a take begins on a prepared machine.

    Included storyboards contribute theirs first: an include is a dependency, and its preparation
    has to be done before the parent's.
    """
    raw = document.get("setup")
    if raw is None:
        entries: list[Any] = []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise StoryboardError("'setup' must be a list of steps")

    steps = list(inherited)
    for entry_number, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise StoryboardError(
                f"setup step {entry_number}: each step is a single-key mapping, got {entry!r}"
            )
        action, args = next(iter(entry.items()))
        if action == "include":
            raise StoryboardError(
                f"setup step {entry_number}: include is only valid under 'steps'; a storyboard's "
                "own setup is inherited automatically when it is included"
            )
        if action not in ALLOWED:
            raise StoryboardError(
                f"setup step {entry_number}: unknown action {action!r}; "
                f"known actions: {sorted(ALLOWED)}"
            )
        if isinstance(args, dict):
            narrated = [key for key in ANNOTATIONS if args.get(key)]
            if narrated:
                raise StoryboardError(
                    f"setup step {entry_number} ({action}): {narrated} do not belong in setup, "
                    "which runs off camera before the recording starts"
                )
        steps.append(
            Step(index=len(steps) + 1, action=action, args=_normalise(action, args, entry_number))
        )
    return tuple(steps)

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
    inherited_setup: list[Step] = []
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
            for child_step in child.setup_steps:
                inherited_setup.append(replace(child_step, index=len(inherited_setup) + 1))
            for child_step in child.steps:
                position += 1
                steps.append(replace(child_step, index=position))
            continue

        # Narration and agenda labels are valid on every action, so strip them before the
        # per-action argument check.
        annotations = {key: "" for key in ANNOTATIONS}
        if isinstance(raw, dict):
            raw = dict(raw)
            for key in ANNOTATIONS:
                annotations[key] = str(raw.pop(key, "") or "").strip()
        if action == "await_operator" and annotations["say_after"]:
            # The run suspends at this step, so say_after would never be spoken.
            raise StoryboardError(
                f"step {entry_number} (await_operator): say_after is never reached, because replay "
                "suspends here; put it on the step that resumes the run"
            )
        position += 1
        steps.append(
            Step(
                index=position,
                action=action,
                args=_normalise(action, raw, entry_number),
                **annotations,
            )
        )

    agenda = document.get("agenda") or {}
    if not isinstance(agenda, dict):
        raise StoryboardError("'agenda' must be a mapping with 'title' and optional 'subtitle'")

    brand = document.get("brand") or {}
    if not isinstance(brand, dict):
        raise StoryboardError("'brand' must be a mapping with optional 'logo' and 'intro' paths")
    unknown = set(brand) - {"logo", "intro"}
    if unknown:
        raise StoryboardError(f"'brand' has unknown key(s) {sorted(unknown)}; expected logo, intro")

    _check_chapters_are_recordable(steps)

    return Storyboard(
        name=str(document.get("name") or (source.stem if source else "storyboard")),
        description=str(document.get("description") or ""),
        beat_between=float(defaults.get("beat", DEFAULT_BEAT)),
        steps=tuple(steps),
        source=source,
        required_values=tuple(sorted(all_required_values)),
        values_source=values_source,
        using_example_values=using_example_values,
        agenda_title=str(agenda.get("title") or "").strip(),
        agenda_subtitle=" ".join(str(agenda.get("subtitle") or "").split()),
        series=" ".join(str(document.get("series") or "").split()),
        brand_logo=_brand_asset(brand.get("logo"), source),
        brand_intro=_brand_asset(brand.get("intro"), source),
        setup_steps=_parse_setup(document, inherited_setup),
    )


def _brand_asset(value: Any, source: Path | None) -> str:
    """Resolve a branding image to an absolute path, so the editor never has to guess a base."""
    if not value:
        return ""
    candidate = Path(str(value)).expanduser()
    if candidate.is_absolute():
        if not candidate.is_file():
            raise StoryboardError(f"brand asset {candidate} does not exist")
        return str(candidate)
    roots = [source.parent, source.parent.parent] if source else [Path.cwd()]
    for root in roots:
        resolved = (root / candidate).resolve()
        if resolved.is_file():
            return str(resolved)
    tried = ", ".join(str((root / candidate).resolve()) for root in roots)
    raise StoryboardError(f"brand asset {value!r} not found; looked in {tried}")


def _check_chapters_are_recordable(steps: list[Step]) -> None:
    """A chapter needs a step that reaches the run manifest, or it vanishes from the edit.

    `await_operator` suspends the replay and is never recorded, so a chapter made only of one
    leaves a gap in the agenda strip. Recording it instead would be worse: a long step with no
    markers is content the editor may never trim.
    """
    seen: dict[str, bool] = {}
    for step in steps:
        if not step.chapter:
            continue
        recordable = step.action != "await_operator"
        seen[step.chapter] = seen.get(step.chapter, False) or recordable
    empty = [title for title, recordable in seen.items() if not recordable]
    if empty:
        raise StoryboardError(
            f"chapter(s) {empty} contain only await_operator steps, which are never recorded; "
            "give each one a real step (a short `beat:` before the wait is enough)"
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
    "show_agenda": lambda d, a: d.show_agenda(float(a.get("seconds", 12))),
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


def _placement(step: Step, position: dict[str, Any]) -> dict[str, Any]:
    """Where a step sits in the agenda, in the shape the run manifest wants: a chapter index and a
    title, rather than the progress fractions the overlay needs."""
    placed: dict[str, Any] = {}
    if position.get("chapter"):
        placed["chapter"] = position["chapter"]
    if step.task:
        placed["task"] = step.task
    if step.task or step.chapter:
        placed["title"] = step.task or step.chapter
    return placed


def step_positions(chapters: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Where each step sits in the agenda, and how full its two bars are when it begins.

    Each step publishes the fraction already earned and the one the step is working towards. The
    overlay eases between them, so a bar moves during a ten-minute install without ever promising
    a finish it cannot know about.
    """
    positions: dict[int, dict[str, Any]] = {}
    for chapter in chapters:
        span = range(chapter["first_step"], chapter["last_step"] + 1)
        for offset, index in enumerate(span):
            positions[index] = {
                "chapter": chapter["index"],
                "task": 0,
                "chapter_progress": round(offset / len(span), 3),
                "chapter_next": round((offset + 1) / len(span), 3),
                "task_progress": 0.0,
                "task_next": 0.0,
            }
        tasks = chapter["tasks"]
        for task in tasks:
            task_span = range(task["first_step"], task["last_step"] + 1)
            for offset, index in enumerate(task_span):
                positions[index] = {
                    "chapter": chapter["index"],
                    "task": task["index"],
                    "chapter_progress": round((task["index"] - 1) / len(tasks), 3),
                    "chapter_next": round(task["index"] / len(tasks), 3),
                    "task_progress": round(offset / len(task_span), 3),
                    "task_next": round((offset + 1) / len(task_span), 3),
                }
    return positions


class _Timeline:
    """Publishes agenda position for the on-camera timeline bar.

    Deliberately swallows every failure and stops trying after the first one: a take must not die
    because a cosmetic overlay could not be written.
    """

    def __init__(self, driver: Any, agenda: dict[str, Any] | None) -> None:
        self._driver = driver if agenda and agenda["chapters"] else None
        self._last: tuple[Any, ...] | None = None
        if self._driver is not None:
            self._try(lambda: self._driver.publish_agenda(agenda))

    def _try(self, call: Callable[[], Any]) -> None:
        if self._driver is None:
            return
        try:
            call()
        except Exception:  # noqa: BLE001 - the overlay is never worth failing a run for
            self._driver = None

    def at(self, position: dict[str, Any], status: str, note: str = "") -> None:
        state = (position["chapter"], position["task"], status, note)
        if state == self._last:
            return
        self._last = state
        self._try(lambda: self._driver.set_overlay(status=status, note=note, **position))

    def finish(self, status: str, note: str = "") -> None:
        self._last = None
        self._try(lambda: self._driver.set_overlay(status=status, note=note))


def run(
    storyboard: Storyboard,
    driver: Any,
    from_step: int = 1,
    to_step: int = 0,
    phase: str = "steps",
    durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Replay every beat in order, stopping at the first failure and saying which step broke.

    An await_operator step suspends instead of blocking, so a human can do something out of band
    (sign in, approve a prompt) and the caller resumes with from_step. to_step stops early on
    purpose, which is how a caller runs a storyboard in segments - to pause a recording between
    them, for instance.

    phase="setup" replays the storyboard's `setup:` list instead: off-camera preparation, with no
    agenda, no timeline and no narration.
    """
    storyboard.require_configured()
    all_steps = storyboard.phase_steps(phase)

    results: list[dict[str, Any]] = []
    cues: list[dict[str, Any]] = []
    started = time.monotonic()
    last = to_step if to_step > 0 else len(all_steps)
    pending = [s for s in all_steps if from_step <= s.index <= last]
    positions = step_positions(storyboard.chapters()) if phase == "steps" else {}
    nowhere = {
        "chapter": 0,
        "task": 0,
        "chapter_progress": 0.0,
        "chapter_next": 0.0,
        "task_progress": 0.0,
        "task_next": 0.0,
    }
    timeline = _Timeline(driver, storyboard.agenda() if phase == "steps" else None)
    measured = durations or {}

    def add_cue(step: Step, kind: str, text: str, at: float) -> None:
        cue_id = f"s{step.index}-{kind}"
        # A step can outrun its own `say`, so a say_after would start mid-sentence. One voice, one
        # line at a time: a cue never starts before the previous one has finished.
        if cues:
            at = max(at, float(cues[-1]["end"]) + CUE_GAP)
        cues.append(
            {
                "id": cue_id,
                "step": step.index,
                "kind": kind,
                "start": round(at, 2),
                "end": round(at + transcript.cue_duration(text, measured.get(cue_id, 0.0)), 2),
                "text": text,
            }
        )

    for step in pending:
        position = positions.get(step.index, nowhere)
        if step.action == "await_operator":
            if step.say:
                add_cue(step, "say", step.say, time.monotonic() - started)
            timeline.at(position, "waiting", str(step.args["message"]))
            return {
                "storyboard": storyboard.name,
                "phase": phase,
                "completed": False,
                "suspended": True,
                "at_step": step.index,
                "message": str(step.args["message"]),
                "guest_log_tail": _guest_tail(driver, int(step.args.get("log_tail", 25))),
                "resume_with": {
                    "name": storyboard.name,
                    "from_step": step.index + 1,
                    "phase": phase,
                },
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "steps": results,
                "transcript": cues,
            }

        step_started = time.monotonic()
        step_started_wall = time.time()
        if step.say:
            add_cue(step, "say", step.say, step_started - started)
        timeline.at(position, "running")
        try:
            detail = ACTIONS[step.action](driver, step.args)
        except Exception as exc:  # noqa: BLE001 - the report is the error channel
            timeline.at(position, "failed", str(exc))
            results.append(
                {
                    "step": step.index,
                    "action": step.action,
                    "args": step.args,
                    "at_seconds": round(step_started - started, 2),
                    "seconds": round(time.monotonic() - step_started, 2),
                    **_placement(step, position),
                    "start_wall": round(step_started_wall, 3),
                    "end_wall": round(time.time(), 3),
                    "ok": False,
                    "error": str(exc),
                }
            )
            return {
                "storyboard": storyboard.name,
                "phase": phase,
                "completed": False,
                "failed_step": step.index,
                "failed_action": step.action,
                "error": str(exc),
                "resume_with": {
                    "name": storyboard.name,
                    "from_step": step.index,
                    "phase": phase,
                },
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "steps": results,
                "transcript": cues,
            }

        if step.say_after:
            add_cue(step, "say-after", step.say_after, time.monotonic() - started)

        results.append(
            {
                "step": step.index,
                "action": step.action,
                "args": step.args,
                "at_seconds": round(step_started - started, 2),
                "seconds": round(time.monotonic() - step_started, 2),
                **_placement(step, position),
                "start_wall": round(step_started_wall, 3),
                "end_wall": round(time.time(), 3),
                "ok": True,
                "detail": _summarise(detail),
            }
        )
        # An explicit beat already holds the shot; adding another would double it.
        hold = storyboard.beat_between if step.action != "beat" else 0.0
        # A cue that outlives its step would collide with the next one, and the narration track
        # can only resolve that by clipping it mid-sentence. Hold the shot until it has finished.
        speaking = max((cue["end"] for cue in cues), default=0.0) - (time.monotonic() - started)
        hold = max(hold, speaking)
        if hold > 0:
            time.sleep(hold)

    finished = last >= len(all_steps)
    timeline.finish("done" if finished else "paused")
    return {
        "storyboard": storyboard.name,
        "phase": phase,
        "completed": finished,
        "stopped_early": not finished,
        "steps_run": len(results),
        "resume_with": None
        if finished
        else {"name": storyboard.name, "from_step": last + 1, "phase": phase},
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
