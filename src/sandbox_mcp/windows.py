"""Win32 helpers: DPI awareness, window discovery, focus, sizing and process control.

`ensure_dpi_awareness()` is carried over verbatim from the Snagit server; without it
`GetWindowRect` and injected mouse coordinates disagree on scaled displays.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Any, Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SW_RESTORE = 9
GW_OWNER = 4
MONITORINFOF_PRIMARY = 1
DWMWA_EXTENDED_FRAME_BOUNDS = 9

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
TH32CS_SNAPPROCESS = 0x0002

SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x0002

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
WM_CLOSE = 0x0010

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


class MONITORINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEX)]
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetGUIThreadInfo.restype = wintypes.BOOL

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]


def ensure_dpi_awareness() -> str:
    """Report physical pixels from GetWindowRect so injected coordinates line up."""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return "per-monitor-v2"
    except AttributeError:
        pass
    try:
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:
            return "per-monitor"
    except Exception:  # noqa: BLE001
        pass
    return "system" if user32.SetProcessDPIAware() else "unaware"


def _rect_to_dict(rect: wintypes.RECT) -> dict[str, int]:
    return {
        "x": rect.left,
        "y": rect.top,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _process_name(hwnd: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, window_pid(hwnd))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def get_window_bounds(hwnd: int) -> dict[str, int]:
    """Visible frame bounds, excluding the invisible drop-shadow border when available."""
    rect = wintypes.RECT()
    if (
        dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        != 0
    ):
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return _rect_to_dict(rect)


def client_size(hwnd: int) -> dict[str, int]:
    """Render area only - this is what the guest desktop is scaled into."""
    rect = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    return {"width": rect.right - rect.left, "height": rect.bottom - rect.top}


def client_origin(hwnd: int) -> dict[str, int]:
    """Screen coordinates of client (0, 0); the origin for guest-relative input."""
    point = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(point))
    return {"x": int(point.x), "y": int(point.y)}


def window_info(hwnd: int) -> dict[str, Any]:
    return {
        "handle": int(hwnd),
        "title": _window_text(hwnd),
        "process": _process_name(hwnd),
        "class_name": _class_name(hwnd),
        "pid": window_pid(hwnd),
        "minimized": bool(user32.IsIconic(hwnd)),
        "maximized": bool(user32.IsZoomed(hwnd)),
        "bounds": get_window_bounds(hwnd),
        "client_origin": client_origin(hwnd),
        "client_size": client_size(hwnd),
    }


def windows_for_process(process_name: str, min_size: int = 100) -> list[dict[str, Any]]:
    """Top-level, unowned, visible windows belonging to a process image name."""
    needle = process_name.lower()
    results: list[dict[str, Any]] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        bounds = get_window_bounds(hwnd)
        if bounds["width"] < min_size or bounds["height"] < min_size:
            return True
        if _process_name(hwnd).lower() != needle:
            return True
        results.append(window_info(hwnd))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return results


def is_foreground(hwnd: int) -> bool:
    return int(user32.GetForegroundWindow() or 0) == int(hwnd)


def find_descendant(hwnd: int, class_name: str) -> int | None:
    needle = class_name.lower()
    found: list[int] = []

    def callback(child: int, _lparam: int) -> bool:
        if _class_name(child).lower() == needle:
            found.append(child)
            return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(callback), 0)
    return found[0] if found else None


def focused_child(hwnd: int) -> int:
    """Which window inside the owning thread actually has the keyboard."""
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    thread = user32.GetWindowThreadProcessId(hwnd, None)
    if not thread or not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        return 0
    return int(info.hwndFocus or 0)


def set_child_focus(parent: int, child: int) -> bool:
    current_thread = kernel32.GetCurrentThreadId()
    thread = user32.GetWindowThreadProcessId(parent, None)
    attached = bool(
        thread and thread != current_thread and user32.AttachThreadInput(current_thread, thread, True)
    )
    try:
        user32.SetFocus(child)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, thread, False)
    return focused_child(parent) == int(child)


def _with_foreground_lock_disabled(action: Callable[[], None]) -> None:
    """SetForegroundWindow is refused for background processes unless the lock timeout is 0."""
    previous = wintypes.DWORD()
    read = user32.SystemParametersInfoW(
        SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(previous), 0
    )
    if read:
        user32.SystemParametersInfoW(
            SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_SENDCHANGE
        )
    try:
        action()
    finally:
        if read:
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(previous.value), SPIF_SENDCHANGE
            )


def _raise_window(hwnd: int) -> None:
    current_thread = kernel32.GetCurrentThreadId()
    foreground = int(user32.GetForegroundWindow() or 0)
    threads = {user32.GetWindowThreadProcessId(hwnd, None)}
    if foreground:
        threads.add(user32.GetWindowThreadProcessId(foreground, None))
    attached = [
        t
        for t in threads
        if t and t != current_thread and user32.AttachThreadInput(current_thread, t, True)
    ]
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
    finally:
        for thread in attached:
            user32.AttachThreadInput(current_thread, thread, False)


def force_foreground(hwnd: int, attempts: int = 3) -> bool:
    if not user32.IsWindow(hwnd):
        raise ValueError(f"Window handle {hwnd} does not exist")
    for attempt in range(max(1, attempts)):
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        _with_foreground_lock_disabled(lambda: _raise_window(hwnd))
        if is_foreground(hwnd):
            return True
        time.sleep(0.15 * (attempt + 1))
    return is_foreground(hwnd)


def set_window_client_size(hwnd: int, width: int, height: int) -> dict[str, int]:
    """Pin the render area to a fixed size and centre it on the primary monitor."""
    if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)
    frame = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(frame))
    client = client_size(hwnd)
    target_w = int(width) + (frame.right - frame.left) - client["width"]
    target_h = int(height) + (frame.bottom - frame.top) - client["height"]
    work = primary_monitor_work_area()
    x = work["x"] + max(0, (work["width"] - target_w) // 2)
    y = work["y"] + max(0, (work["height"] - target_h) // 2)
    user32.SetWindowPos(hwnd, None, x, y, target_w, target_h, SWP_NOZORDER | SWP_NOACTIVATE)
    time.sleep(0.2)
    return client_size(hwnd)


def close_window(hwnd: int) -> None:
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def list_monitors() -> list[dict[str, Any]]:
    monitors: list[dict[str, Any]] = []

    def callback(hmonitor: int, _hdc: int, _rect: Any, _lparam: int) -> bool:
        info = MONITORINFOEX()
        info.cbSize = ctypes.sizeof(MONITORINFOEX)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            monitors.append(
                {
                    "device": info.szDevice,
                    "primary": bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    "bounds": _rect_to_dict(info.rcMonitor),
                    "work_area": _rect_to_dict(info.rcWork),
                }
            )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    return monitors


def primary_monitor_work_area() -> dict[str, int]:
    for monitor in list_monitors():
        if monitor["primary"]:
            return monitor["work_area"]
    return virtual_screen_bounds()


def virtual_screen_bounds() -> dict[str, int]:
    return {
        "x": user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        "y": user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        "width": user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        "height": user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    }


def find_processes(*names: str) -> list[dict[str, Any]]:
    wanted = {name.lower() for name in names}
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return []
    found: list[dict[str, Any]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.szExeFile.lower() in wanted:
                found.append({"pid": int(entry.th32ProcessID), "name": entry.szExeFile})
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def terminate_process(pid: int) -> bool:
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def wait_for_processes_gone(names: tuple[str, ...], timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not find_processes(*names):
            return True
        time.sleep(0.3)
    return not find_processes(*names)
