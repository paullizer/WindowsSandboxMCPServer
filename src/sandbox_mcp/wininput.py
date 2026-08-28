"""Synthetic keyboard and mouse input via SendInput, plus host clipboard handling.

Keys are injected as scancodes (KEYEVENTF_SCANCODE) rather than virtual-key codes, because
virtual-key injection is measurably less reliable across the sandbox's RDP channel. Characters
with no US-layout scancode fall back to KEYEVENTF_UNICODE.
"""

from __future__ import annotations

import ctypes
import random
import re
import time
from contextlib import contextmanager
from ctypes import wintypes
from typing import Iterator

from .windows import kernel32, user32, virtual_screen_bounds

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x0001, 0x0002
KEYEVENTF_UNICODE, KEYEVENTF_SCANCODE = 0x0004, 0x0008

MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_VIRTUALDESK, MOUSEEVENTF_ABSOLUTE = 0x0800, 0x4000, 0x8000
WHEEL_DELTA = 120

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# Zero-delay bursts get dropped by the RDP channel.
KEY_DELAY = 0.02
MODIFIER_DELAY = 0.03

ULONG_PTR = wintypes.WPARAM


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

# Set 1 scancodes for a US layout.
SCANCODES: dict[str, int] = {
    "escape": 0x01, "minus": 0x0C, "equals": 0x0D, "backspace": 0x0E, "tab": 0x0F,
    "leftbracket": 0x1A, "rightbracket": 0x1B, "enter": 0x1C, "ctrl": 0x1D,
    "semicolon": 0x27, "quote": 0x28, "backquote": 0x29, "shift": 0x2A, "backslash": 0x2B,
    "comma": 0x33, "period": 0x34, "slash": 0x35, "rightshift": 0x36,
    "alt": 0x38, "space": 0x39, "capslock": 0x3A, "numlock": 0x45, "scrolllock": 0x46,
    "f11": 0x57, "f12": 0x58,
}
SCANCODES.update(dict(zip("1234567890", range(0x02, 0x0C))))
SCANCODES.update(dict(zip("qwertyuiop", range(0x10, 0x1A))))
SCANCODES.update(dict(zip("asdfghjkl", range(0x1E, 0x27))))
SCANCODES.update(dict(zip("zxcvbnm", range(0x2C, 0x33))))
SCANCODES.update({f"f{i}": 0x3A + i for i in range(1, 11)})

# Extended keys need the E0 prefix, which KEYEVENTF_EXTENDEDKEY adds for us.
EXTENDED: dict[str, int] = {
    "up": 0x48, "down": 0x50, "left": 0x4B, "right": 0x4D,
    "insert": 0x52, "delete": 0x53, "home": 0x47, "end": 0x4F,
    "pageup": 0x49, "pagedown": 0x51,
    "win": 0x5B, "rightwin": 0x5C, "menu": 0x5D,
    "rightctrl": 0x1D, "rightalt": 0x38, "numpadenter": 0x1C, "printscreen": 0x37,
}

ALIASES = {
    "esc": "escape", "return": "enter", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "pagedn": "pagedown",
    "control": "ctrl", "lctrl": "ctrl", "rctrl": "rightctrl",
    "lshift": "shift", "rshift": "rightshift",
    "lalt": "alt", "ralt": "rightalt", "altgr": "rightalt",
    "cmd": "win", "super": "win", "windows": "win", "lwin": "win", "rwin": "rightwin",
    "spacebar": "space", "bksp": "backspace", "arrowup": "up", "arrowdown": "down",
    "arrowleft": "left", "arrowright": "right", "apps": "menu", "context": "menu",
    "-": "minus", "=": "equals", "[": "leftbracket", "]": "rightbracket",
    ";": "semicolon", "'": "quote", "`": "backquote", "\\": "backslash",
    ",": "comma", ".": "period", "/": "slash", " ": "space",
}

MODIFIERS = {"ctrl", "alt", "shift", "win", "rightctrl", "rightalt", "rightshift", "rightwin"}

CHARS: dict[str, tuple[str, bool]] = {
    " ": ("space", False), "\t": ("tab", False), "\n": ("enter", False),
    "-": ("minus", False), "_": ("minus", True),
    "=": ("equals", False), "+": ("equals", True),
    "[": ("leftbracket", False), "{": ("leftbracket", True),
    "]": ("rightbracket", False), "}": ("rightbracket", True),
    ";": ("semicolon", False), ":": ("semicolon", True),
    "'": ("quote", False), '"': ("quote", True),
    "`": ("backquote", False), "~": ("backquote", True),
    "\\": ("backslash", False), "|": ("backslash", True),
    ",": ("comma", False), "<": ("comma", True),
    ".": ("period", False), ">": ("period", True),
    "/": ("slash", False), "?": ("slash", True),
    "!": ("1", True), "@": ("2", True), "#": ("3", True), "$": ("4", True),
    "%": ("5", True), "^": ("6", True), "&": ("7", True), "*": ("8", True),
    "(": ("9", True), ")": ("0", True),
}
CHARS.update({c: (c, False) for c in "abcdefghijklmnopqrstuvwxyz0123456789"})
CHARS.update({c.upper(): (c, True) for c in "abcdefghijklmnopqrstuvwxyz"})

# Deliberately narrow so ordinary commands and URLs are not blocked.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("credential assignment", re.compile(
        r"(?i)\b(password|passwd|pwd|client[_-]?secret|api[_-]?key|access[_-]?key|"
        r"account[_-]?key|secret[_-]?key|sas[_-]?token)\b\s*[:=]\s*\S")),
    ("json web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("github token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws access key id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
]


class InputError(RuntimeError):
    pass


def detect_secret(text: str) -> str | None:
    """Name the credential pattern this text matches, so it can be refused on camera."""
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return label
    return None


def resolve_key(name: str) -> tuple[int, bool]:
    key = ALIASES.get(name.lower(), name.lower())
    if key in EXTENDED:
        return EXTENDED[key], True
    if key in SCANCODES:
        return SCANCODES[key], False
    raise InputError(f"Unknown key name: {name!r}")


def _send(*inputs: INPUT) -> None:
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise InputError(f"SendInput accepted {sent} of {len(inputs)} events")


def _key_event(scan: int, extended: bool, up: bool) -> INPUT:
    flags = KEYEVENTF_SCANCODE
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= KEYEVENTF_KEYUP
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def _unicode_event(code: int, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def key_down(name: str) -> None:
    scan, extended = resolve_key(name)
    _send(_key_event(scan, extended, False))


def key_up(name: str) -> None:
    scan, extended = resolve_key(name)
    _send(_key_event(scan, extended, True))


def tap_key(name: str, hold: float = KEY_DELAY) -> None:
    key_down(name)
    time.sleep(hold)
    key_up(name)


def parse_chord(chord: str) -> tuple[list[str], str]:
    parts = [part.strip().lower() for part in chord.split("+") if part.strip()]
    if not parts:
        raise InputError(f"Empty key chord: {chord!r}")
    resolved = [ALIASES.get(part, part) for part in parts]
    modifiers = [part for part in resolved[:-1] if part in MODIFIERS]
    if len(modifiers) != len(resolved) - 1:
        raise InputError(f"Only modifiers may precede the key in a chord: {chord!r}")
    return modifiers, resolved[-1]


def send_chord(chord: str, hold: float = KEY_DELAY) -> dict[str, object]:
    modifiers, key = parse_chord(chord)
    resolve_key(key)
    for modifier in modifiers:
        key_down(modifier)
        time.sleep(MODIFIER_DELAY)
    try:
        tap_key(key, hold)
    finally:
        for modifier in reversed(modifiers):
            time.sleep(MODIFIER_DELAY)
            key_up(modifier)
    return {"modifiers": modifiers, "key": key}


def type_text(text: str, cps: float = 14.0, jitter: float = 0.35) -> int:
    """Type visibly, at a human pace, one character at a time."""
    if cps <= 0:
        raise InputError("cps must be greater than 0")
    interval = 1.0 / cps
    typed = 0
    for char in text:
        if char == "\r":
            continue
        mapped = CHARS.get(char)
        if mapped is None:
            code = ord(char)
            _send(_unicode_event(code, False))
            time.sleep(KEY_DELAY)
            _send(_unicode_event(code, True))
        else:
            key, shift = mapped
            if shift:
                key_down("shift")
                time.sleep(KEY_DELAY)
            try:
                tap_key(key)
            finally:
                if shift:
                    time.sleep(KEY_DELAY)
                    key_up("shift")
        typed += 1
        time.sleep(max(0.0, interval * (1.0 + random.uniform(-jitter, jitter))))
    return typed


@contextmanager
def _clipboard(retries: int = 12, delay: float = 0.06) -> Iterator[None]:
    """The clipboard is a shared lock; other apps hold it briefly all the time."""
    for attempt in range(retries):
        if user32.OpenClipboard(None):
            try:
                yield
            finally:
                user32.CloseClipboard()
            return
        time.sleep(delay * (attempt + 1))
    raise InputError("Could not open the clipboard; another application is holding it")


def get_clipboard_text() -> str | None:
    with _clipboard():
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)


def set_clipboard_text(text: str) -> None:
    buffer = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buffer)
    with _clipboard():
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise InputError("GlobalAlloc failed while setting the clipboard")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise InputError("GlobalLock failed while setting the clipboard")
        ctypes.memmove(pointer, buffer, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise InputError("SetClipboardData failed")


def paste_text(text: str, restore: bool = True, settle: float = 0.6) -> dict[str, object]:
    """Set the clipboard, send ctrl+v, then hand the host clipboard back."""
    previous = None
    if restore:
        try:
            previous = get_clipboard_text()
        except InputError:
            previous = None
    set_clipboard_text(text)
    time.sleep(0.15)
    send_chord("ctrl+v")
    time.sleep(settle)
    restored = False
    if restore and previous is not None:
        set_clipboard_text(previous)
        restored = True
    return {"characters": len(text), "clipboard_restored": restored}


def cursor_position() -> tuple[int, int]:
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


def _absolute_move_event(screen_x: int, screen_y: int) -> INPUT:
    bounds = virtual_screen_bounds()
    width = max(1, bounds["width"] - 1)
    height = max(1, bounds["height"] - 1)
    normalized_x = round((screen_x - bounds["x"]) * 65535 / width)
    normalized_y = round((screen_y - bounds["y"]) * 65535 / height)
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(
        dx=max(0, min(65535, normalized_x)),
        dy=max(0, min(65535, normalized_y)),
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0,
        dwExtraInfo=0,
    )
    return event


def move_mouse_to(screen_x: int, screen_y: int, duration_ms: int = 250) -> None:
    """Travel there smoothly - instant cursor jumps read as broken on video."""
    start_x, start_y = cursor_position()
    steps = max(1, int(duration_ms / 12))
    for step in range(1, steps + 1):
        progress = step / steps
        eased = progress * progress * (3 - 2 * progress)
        _send(
            _absolute_move_event(
                round(start_x + (screen_x - start_x) * eased),
                round(start_y + (screen_y - start_y) * eased),
            )
        )
        time.sleep(duration_ms / 1000.0 / steps)


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def _button_event(flag: int) -> INPUT:
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=0)
    return event


def click_at(
    screen_x: int,
    screen_y: int,
    button: str = "left",
    double: bool = False,
    move_duration_ms: int = 250,
) -> None:
    if button not in _BUTTONS:
        raise InputError(f"Unknown mouse button: {button!r}")
    down, up = _BUTTONS[button]
    move_mouse_to(screen_x, screen_y, move_duration_ms)
    time.sleep(0.08)
    for index in range(2 if double else 1):
        if index:
            time.sleep(0.08)
        _send(_button_event(down))
        time.sleep(0.04)
        _send(_button_event(up))


def scroll_wheel(notches: int) -> None:
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(
        dx=0,
        dy=0,
        mouseData=ctypes.c_uint32(int(notches) * WHEEL_DELTA).value,
        dwFlags=MOUSEEVENTF_WHEEL,
        time=0,
        dwExtraInfo=0,
    )
    _send(event)
