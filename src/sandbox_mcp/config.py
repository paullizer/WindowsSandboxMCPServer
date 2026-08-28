"""Configuration for the Windows Sandbox MCP server, plus host capability detection."""

from __future__ import annotations

import ctypes
from importlib import metadata
import os
import re
import shutil
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SANDBOX_LAUNCHER = "WindowsSandbox.exe"
# The window belongs to WindowsSandboxRemoteSession.exe on Windows 11 25H2+ (class
# WinUIDesktopWin32WindowClass) and to WindowsSandboxClient.exe on older builds.
SANDBOX_REMOTE_SESSION = "WindowsSandboxRemoteSession.exe"
SANDBOX_CLIENT = "WindowsSandboxClient.exe"
SANDBOX_WINDOW_PROCESSES = (SANDBOX_REMOTE_SESSION, SANDBOX_CLIENT)
SANDBOX_PROCESSES = SANDBOX_WINDOW_PROCESSES + (SANDBOX_LAUNCHER,)
# The VM keeps running for a few seconds after the client dies, and a new sandbox cannot start
# until it is reclaimed.
SANDBOX_VM_PROCESSES = ("vmmemWindowsSandbox", "vmmemWindowsSandbox.exe")

# Windows Sandbox is installed as the "Containers-DisposableClientVM" optional feature;
# CmService is the service it brings with it.
SANDBOX_SERVICES = ("CmService", "vmcompute", "HvHost")

PF_SECOND_LEVEL_ADDRESS_TRANSLATION = 20
PF_VIRT_FIRMWARE_ENABLED = 21

_SERVICE_STATES = {
    1: "stopped",
    2: "start_pending",
    3: "stop_pending",
    4: "running",
    5: "continue_pending",
    6: "pause_pending",
    7: "paused",
}

_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Settings:
    work_dir: Path
    payload_dir: Path | None
    storyboard_dir: Path | None
    memory_mb: int
    networking: bool
    vgpu: bool
    window_width: int
    window_height: int
    boot_timeout_seconds: float
    max_wait_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        raw_work = os.environ.get("SANDBOX_MCP_WORK_DIR")
        work_dir = Path(raw_work).expanduser() if raw_work else _default_work_dir()
        raw_payload = os.environ.get("SANDBOX_MCP_PAYLOAD_DIR")
        payload_dir = (
            Path(raw_payload).expanduser().resolve() if raw_payload else _resource_dir("payload")
        )
        raw_storyboards = os.environ.get("SANDBOX_MCP_STORYBOARD_DIR")
        storyboard_dir = (
            Path(raw_storyboards).expanduser().resolve()
            if raw_storyboards
            else _resource_dir("storyboards")
        )
        width, height = _parse_size(os.environ.get("SANDBOX_MCP_WINDOW_SIZE", "1600x900"))
        return cls(
            work_dir=work_dir.resolve(),
            payload_dir=payload_dir,
            storyboard_dir=storyboard_dir,
            memory_mb=int(os.environ.get("SANDBOX_MCP_MEMORY_MB", "8192")),
            networking=_env_flag("SANDBOX_MCP_NETWORKING", True),
            vgpu=_env_flag("SANDBOX_MCP_VGPU", True),
            window_width=width,
            window_height=height,
            boot_timeout_seconds=float(os.environ.get("SANDBOX_MCP_BOOT_TIMEOUT_SECONDS", "120")),
            max_wait_seconds=float(os.environ.get("SANDBOX_MCP_MAX_WAIT_SECONDS", "600")),
        )

    def ensure_work_dir(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self.work_dir

    def list_storyboards(self) -> list[Path]:
        if self.storyboard_dir is None or not self.storyboard_dir.is_dir():
            return []
        return sorted(
            p for p in self.storyboard_dir.iterdir() if p.suffix.lower() in (".yaml", ".yml")
        )

    def resolve_storyboard(self, name: str) -> Path:
        """Accept a path, or a bare name looked up in the storyboard directory."""
        direct = Path(name).expanduser()
        if direct.is_file():
            return direct.resolve()
        if self.storyboard_dir is None:
            raise FileNotFoundError(f"No storyboard directory configured, and {name!r} is not a file")
        # Only the file name is honoured, so a storyboard name cannot walk out of the directory.
        stem = _UNSAFE_NAME.sub("-", Path(name).name)
        for candidate in (stem, f"{stem}.yaml", f"{stem}.yml"):
            target = (self.storyboard_dir / candidate).resolve()
            if target.is_file() and target.parent == self.storyboard_dir:
                return target
        available = [p.stem for p in self.list_storyboards()]
        raise FileNotFoundError(f"No storyboard named {name!r}. Available: {available or 'none'}")

    def session_dir(self, session_id: str) -> Path:
        """Per-session folder holding the generated .wsb and the read-write control channel."""
        if not _SESSION_ID.match(session_id):
            raise ValueError(f"Invalid session id: {session_id!r}")
        target = (self.ensure_work_dir() / "sessions" / session_id).resolve()
        if self.work_dir not in target.parents:
            raise ValueError("Resolved session path escaped the configured work directory")
        target.mkdir(parents=True, exist_ok=True)
        return target


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _parse_size(raw: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*[x,]\s*(\d+)\s*$", raw)
    if not match:
        return 1600, 900
    return int(match.group(1)), int(match.group(2))


def _default_work_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) if local else Path.home() / "AppData" / "Local"
    return root / "sandbox-mcp"


def _repo_dir(name: str) -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / name
    return candidate if candidate.is_dir() else None


def _resource_dir(name: str) -> Path | None:
    source = _repo_dir(name)
    if source is not None:
        return source

    markers = {"payload": "bootstrap.ps1", "storyboards": "example-notepadpp.yaml"}
    marker = markers.get(name)
    if marker is None:
        return None
    try:
        files = metadata.files("windows-sandbox-mcp") or ()
    except metadata.PackageNotFoundError:
        return None
    suffix = f"share/windows-sandbox-mcp/{name}/{marker}"
    for entry in files:
        if entry.as_posix().endswith(suffix):
            candidate = Path(entry.locate()).parent
            return candidate if candidate.is_dir() else None
    return None


def find_windows_sandbox() -> dict[str, Any] | None:
    """Locate WindowsSandbox.exe and its client binaries, or None if the feature is off."""
    windir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    # Sysnative first so a 32-bit interpreter is not redirected into SysWOW64.
    for system_dir in (windir / "Sysnative", windir / "System32"):
        launcher = system_dir / SANDBOX_LAUNCHER
        if launcher.is_file():
            return {
                "launcher": str(launcher),
                "clients": [
                    str(system_dir / name)
                    for name in SANDBOX_WINDOW_PROCESSES
                    if (system_dir / name).is_file()
                ],
            }
    return None


def find_wsb_cli() -> str | None:
    """The newer `wsb` CLI is optional; prefer it when present but never require it."""
    return shutil.which("wsb")


def windows_edition() -> dict[str, Any]:
    values: dict[str, Any] = {}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            for name in ("EditionID", "ProductName", "DisplayVersion", "CurrentBuild", "UBR"):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    values[name] = None
    except OSError:
        pass
    edition = str(values.get("EditionID") or "")
    build = values.get("CurrentBuild")
    return {
        "edition_id": edition or None,
        "product_name": values.get("ProductName"),
        "display_version": values.get("DisplayVersion"),
        "build": f"{build}.{values.get('UBR')}" if build else None,
        # Home ships as Core / CoreN / CoreSingleLanguage and cannot run Windows Sandbox.
        "supported": bool(edition) and not edition.lower().startswith("core"),
    }


def service_state(name: str) -> str:
    try:
        import win32service
    except ImportError:
        return "unknown"
    try:
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except Exception:  # noqa: BLE001 - no access to the SCM is reported, not fatal
        return "unknown"
    try:
        try:
            handle = win32service.OpenService(manager, name, win32service.SERVICE_QUERY_STATUS)
        except Exception:  # noqa: BLE001 - missing service means the feature is not installed
            return "absent"
        try:
            state = win32service.QueryServiceStatus(handle)[1]
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(manager)
    return _SERVICE_STATES.get(state, str(state))


def virtualization_state() -> dict[str, Any]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.IsProcessorFeaturePresent.argtypes = [ctypes.c_uint32]
    kernel32.IsProcessorFeaturePresent.restype = ctypes.c_int
    services = {name: service_state(name) for name in SANDBOX_SERVICES}
    hypervisor_running = any(state == "running" for state in services.values())
    return {
        "slat": bool(kernel32.IsProcessorFeaturePresent(PF_SECOND_LEVEL_ADDRESS_TRANSLATION)),
        "virtualization_firmware_enabled": bool(
            kernel32.IsProcessorFeaturePresent(PF_VIRT_FIRMWARE_ENABLED)
        ),
        "hypervisor_running": hypervisor_running,
        "services": services,
        "note": (
            "slat and virtualization_firmware_enabled are reported by the processor and read "
            "false once a hypervisor owns those bits, so they are only meaningful when "
            "hypervisor_running is false."
        ),
    }
