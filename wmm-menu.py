#!/usr/bin/env python3
"""
wmm-menu.py - Pure-Python Windows OBS Portable Launcher.

Replaces start-obs.cmd with a GUI launcher that:
- Reads/writes a YAML configuration file (default: menu.yaml)
- Displays equal-width buttons for each OBS subfolder with an attached 'Fast' button
- Monitors and toggles Windows services (syncthing, rslsyncsvc)
- Launches OBS in background threads (preventing duplicate launches per item)
- Stops sync services, backs up OBS config to a locale-independent timestamped ZIP
  with live progress, runs OBS in portable mode, reports errors, and restarts services.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import io
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

if sys.platform == "win32":
    import winreg

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "customtkinter is required. Install it with: pip install customtkinter"
    ) from exc

try:
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Pillow is required. Install it with: pip install Pillow"
    ) from exc

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required. Install it with: pip install PyYAML"
    ) from exc


# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------

APP_TITLE = "WMM OBS Launcher"
DEFAULT_CONFIG_FILENAME = "menu.yaml"
DEFAULT_BASE_FOLDER = r"C:\obs-studio"
DEFAULT_SERVICES = ("syncthing", "rslsyncsvc")
OBS_RELATIVE_EXE = Path("bin") / "64bit" / "obs64.exe"
OBS_DEFAULT_ARGS = ["--portable", "--disable-updater"]

SERVICE_POLL_INTERVAL_MS = 4000


def get_app_dir() -> Path:
    """Return the directory containing the script or frozen PyInstaller executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_path(filename: str) -> Path | None:
    """Locate a bundled resource (e.g., app.ico) in PyInstaller _MEIPASS or app dir."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / filename)
    candidates.append(get_app_dir() / filename)
    for path in candidates:
        if path.is_file():
            return path
    return None


def apply_window_icon(window: tk.Misc) -> None:
    """Apply app.ico to a CTk or CTkToplevel window (and re-apply after CTk's delayed init)."""
    icon_path = get_resource_path("app.ico")
    if icon_path is None:
        return

    def _set() -> None:
        if window.winfo_exists():
            try:
                window.iconbitmap(str(icon_path))  # type: ignore[attr-defined]
            except tk.TclError:
                pass

    _set()
    window.after(250, _set)


# ---------------------------------------------------------------------------
# Windows Service Control (Pure Python via Win32 advapi32 / shell32)
# ---------------------------------------------------------------------------

# Win32 SCM constants
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_CONTROL_STOP = 0x00000001

SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SERVICE_CONTINUE_PENDING = 0x00000005
SERVICE_PAUSE_PENDING = 0x00000006
SERVICE_PAUSED = 0x00000007

ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_NOT_ACTIVE = 1062

SEE_MASK_NOCLOSEPROCESS = 0x00000040
INFINITE = 0xFFFFFFFF
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
SPI_GETWORKAREA = 0x0030
MONITOR_DEFAULTTONEAREST = 0x00000002
MDT_EFFECTIVE_DPI = 0


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wintypes.DWORD),
        ("dwCurrentState", wintypes.DWORD),
        ("dwControlsAccepted", wintypes.DWORD),
        ("dwWin32ExitCode", wintypes.DWORD),
        ("dwServiceSpecificExitCode", wintypes.DWORD),
        ("dwCheckPoint", wintypes.DWORD),
        ("dwWaitHint", wintypes.DWORD),
    ]


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


if sys.platform == "win32":
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _version_dll = ctypes.WinDLL("version", use_last_error=True)
    try:
        _shcore: ctypes.WinDLL | None = ctypes.WinDLL("shcore", use_last_error=True)
        _shcore.GetDpiForMonitor.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        _shcore.GetDpiForMonitor.restype = ctypes.c_long
    except OSError:
        _shcore = None

    _user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    _user32.MonitorFromPoint.restype = wintypes.HANDLE

    _user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL

    _user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    _user32.SystemParametersInfoW.restype = wintypes.BOOL

    _version_dll.GetFileVersionInfoSizeW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _version_dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD

    _version_dll.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    _version_dll.GetFileVersionInfoW.restype = wintypes.BOOL

    _version_dll.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    _version_dll.VerQueryValueW.restype = wintypes.BOOL

    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    _shell32.ShellExecuteExW.restype = wintypes.BOOL

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL

    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL

    _advapi32.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _advapi32.OpenSCManagerW.restype = wintypes.HANDLE

    _advapi32.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
    _advapi32.OpenServiceW.restype = wintypes.HANDLE

    _advapi32.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
    _advapi32.QueryServiceStatus.restype = wintypes.BOOL

    _advapi32.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    _advapi32.StartServiceW.restype = wintypes.BOOL

    _advapi32.ControlService.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(SERVICE_STATUS),
    ]
    _advapi32.ControlService.restype = wintypes.BOOL

    _advapi32.CloseServiceHandle.argtypes = [wintypes.HANDLE]
    _advapi32.CloseServiceHandle.restype = wintypes.BOOL


def get_running_process_names() -> set[str]:
    """Return lowercase executable filenames of all currently running Windows processes."""
    if sys.platform != "win32":
        return set()
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return set()
    names: set[str] = set()
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if _kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                names.add(pe.szExeFile.lower())
                if not _kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        _kernel32.CloseHandle(snap)
    return names


class SyncTrayzorController:
    """Detect and control user-installed SyncTrayzor in place of the syncthing service."""

    _cached_exe: Path | None = None
    _checked: bool = False

    @classmethod
    def find_executable(cls) -> Path | None:
        """Locate SyncTrayzor.exe via standard install paths or Windows Uninstall registry."""
        if sys.platform != "win32":
            return None
        if cls._checked:
            if cls._cached_exe is not None and cls._cached_exe.is_file():
                return cls._cached_exe
            return None

        candidates: list[Path] = []
        env_dirs = [
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("APPDATA"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
        ]
        if env_dirs[0]:
            candidates.append(Path(env_dirs[0]) / "Programs" / "SyncTrayzor" / "SyncTrayzor.exe")
            candidates.append(Path(env_dirs[0]) / "SyncTrayzor" / "SyncTrayzor.exe")
        if env_dirs[1]:
            candidates.append(Path(env_dirs[1]) / "SyncTrayzor" / "SyncTrayzor.exe")
        if env_dirs[2]:
            candidates.append(Path(env_dirs[2]) / "SyncTrayzor" / "SyncTrayzor.exe")
        if env_dirs[3]:
            candidates.append(Path(env_dirs[3]) / "SyncTrayzor" / "SyncTrayzor.exe")

        for path in candidates:
            if path.is_file():
                cls._cached_exe = path
                cls._checked = True
                return path

        # Check Windows Uninstall Registry (HKCU + HKLM)
        reg_roots = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ),
        ]
        for hive, subkey_path in reg_roots:
            try:
                with winreg.OpenKey(hive, subkey_path) as root_key:
                    idx = 0
                    while True:
                        try:
                            child_name = winreg.EnumKey(root_key, idx)
                            idx += 1
                        except OSError:
                            break
                        try:
                            with winreg.OpenKey(root_key, child_name) as item_key:
                                disp, _ = winreg.QueryValueEx(item_key, "DisplayName")
                                if "synctrayzor" in str(disp).lower():
                                    loc, _ = winreg.QueryValueEx(item_key, "InstallLocation")
                                    exe = Path(str(loc).strip('" ')) / "SyncTrayzor.exe"
                                    if exe.is_file():
                                        cls._cached_exe = exe
                                        cls._checked = True
                                        return exe
                        except OSError:
                            continue
            except OSError:
                continue

        cls._checked = True
        cls._cached_exe = None
        return None

    @classmethod
    def is_installed(cls) -> bool:
        return cls.find_executable() is not None

    @classmethod
    def get_status(cls) -> str:
        """Return 'running' if syncthing.exe is active, 'stopped' if installed, else 'n/a'."""
        if not cls.is_installed():
            return "n/a"
        procs = get_running_process_names()
        return "running" if "syncthing.exe" in procs else "stopped"

    @classmethod
    def _wait_for_state(cls, target: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls.get_status() == target:
                return True
            time.sleep(0.3)
        return cls.get_status() == target

    @classmethod
    def start(cls) -> tuple[bool, str]:
        exe = cls.find_executable()
        if exe is None:
            return False, "SyncTrayzor is not installed."
        if cls.get_status() == "running":
            return True, "SyncTrayzor (Syncthing) is already running."

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(
                [str(exe), "--minimized", "--start-syncthing"],
                cwd=str(exe.parent),
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return False, f"Failed to start SyncTrayzor:\n{exc}"

        if cls._wait_for_state("running", timeout=20.0):
            return True, "SyncTrayzor started Syncthing."
        return False, "Timed out waiting for SyncTrayzor to start Syncthing."

    @classmethod
    def stop(cls) -> tuple[bool, str]:
        exe = cls.find_executable()
        if exe is None:
            return False, "SyncTrayzor is not installed."
        if cls.get_status() == "stopped":
            return True, "SyncTrayzor (Syncthing) is already stopped."

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            subprocess.Popen(
                [str(exe), "--minimized", "--stop-syncthing"],
                cwd=str(exe.parent),
                creationflags=flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return False, f"Failed to stop SyncTrayzor:\n{exc}"

        if cls._wait_for_state("stopped", timeout=20.0):
            return True, "SyncTrayzor stopped Syncthing."
        return False, "Timed out waiting for SyncTrayzor to stop Syncthing."


class WindowsServiceController:
    """Query, start, and stop Windows services (with SyncTrayzor support for syncthing)."""

    @staticmethod
    def get_raw_service_status(service_name: str) -> str:
        """Query Win32 SCM directly; return 'running', 'stopped', 'pending', or 'n/a'."""
        if sys.platform != "win32":
            return "n/a"

        scm = _advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not scm:
            return "n/a"
        try:
            svc = _advapi32.OpenServiceW(scm, service_name, SERVICE_QUERY_STATUS)
            if not svc:
                err = ctypes.get_last_error()
                if err == ERROR_SERVICE_DOES_NOT_EXIST:
                    return "n/a"
                return "n/a"
            try:
                status = SERVICE_STATUS()
                if not _advapi32.QueryServiceStatus(svc, ctypes.byref(status)):
                    return "n/a"
                state = status.dwCurrentState
                if state == SERVICE_RUNNING:
                    return "running"
                if state in (SERVICE_STOPPED, SERVICE_PAUSED):
                    return "stopped"
                if state in (
                    SERVICE_START_PENDING,
                    SERVICE_STOP_PENDING,
                    SERVICE_CONTINUE_PENDING,
                    SERVICE_PAUSE_PENDING,
                ):
                    return "pending"
                return "stopped"
            finally:
                _advapi32.CloseServiceHandle(svc)
        finally:
            _advapi32.CloseServiceHandle(scm)

    @classmethod
    def uses_synctrayzor(cls, service_name: str) -> bool:
        """Return True if `service_name` is 'syncthing' and should be controlled via SyncTrayzor."""
        if service_name.lower() != "syncthing":
            return False
        if not SyncTrayzorController.is_installed():
            return False
        if "synctrayzor.exe" in get_running_process_names():
            return True
        return cls.get_raw_service_status("syncthing") == "n/a"

    @classmethod
    def get_display_name(cls, service_name: str) -> str:
        return "SyncTrayzor" if cls.uses_synctrayzor(service_name) else service_name

    @classmethod
    def get_status(cls, service_name: str) -> str:
        """Return 'running', 'stopped', 'pending', or 'n/a'."""
        if cls.uses_synctrayzor(service_name):
            return SyncTrayzorController.get_status()
        return cls.get_raw_service_status(service_name)

    @classmethod
    def _wait_for_state(cls, service_name: str, target: str, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cls.get_raw_service_status(service_name) == target:
                return True
            time.sleep(0.3)
        return cls.get_raw_service_status(service_name) == target

    @classmethod
    def _elevated_sc(cls, action: str, service_name: str, target: str) -> tuple[bool, str]:
        """Fallback to UAC-elevated sc.exe when direct SCM access returns Access Denied."""
        ret = _shell32.ShellExecuteW(
            None,
            "runas",
            "sc.exe",
            f'{action} "{service_name}"',
            None,
            0,  # SW_HIDE
        )
        if ret <= 32:
            return False, f"Elevation cancelled or failed (ShellExecuteW code {ret})."
        if cls._wait_for_state(service_name, target, timeout=15.0):
            return True, f"Service '{service_name}' is now {target}."
        return False, f"Timed out waiting for '{service_name}' to become {target}."

    @classmethod
    def start_service(cls, service_name: str) -> tuple[bool, str]:
        """Start a Windows service (or SyncTrayzor). Returns (success, message)."""
        if sys.platform != "win32":
            return False, "Windows service control is only supported on Windows."

        if cls.uses_synctrayzor(service_name):
            return SyncTrayzorController.start()

        current = cls.get_raw_service_status(service_name)
        if current == "n/a":
            return False, f"Service '{service_name}' is not installed."
        if current == "running":
            return True, f"Service '{service_name}' is already running."

        scm = _advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not scm:
            return False, f"Unable to open Service Control Manager (error {ctypes.get_last_error()})."
        try:
            svc = _advapi32.OpenServiceW(scm, service_name, SERVICE_START | SERVICE_QUERY_STATUS)
            if not svc:
                err = ctypes.get_last_error()
                if err == ERROR_ACCESS_DENIED:
                    return cls._elevated_sc("start", service_name, "running")
                if err == ERROR_SERVICE_DOES_NOT_EXIST:
                    return False, f"Service '{service_name}' is not installed."
                return False, f"Failed to open service '{service_name}' (error {err})."
            try:
                if not _advapi32.StartServiceW(svc, 0, None):
                    err = ctypes.get_last_error()
                    if err == ERROR_SERVICE_ALREADY_RUNNING:
                        return True, f"Service '{service_name}' is already running."
                    if err == ERROR_ACCESS_DENIED:
                        return cls._elevated_sc("start", service_name, "running")
                    return False, f"Failed to start '{service_name}' (error {err})."
            finally:
                _advapi32.CloseServiceHandle(svc)
        finally:
            _advapi32.CloseServiceHandle(scm)

        if cls._wait_for_state(service_name, "running", timeout=20.0):
            return True, f"Service '{service_name}' started."
        return False, f"Timed out waiting for '{service_name}' to start."

    @classmethod
    def stop_service(cls, service_name: str) -> tuple[bool, str]:
        """Stop a Windows service (or SyncTrayzor). Returns (success, message)."""
        if sys.platform != "win32":
            return False, "Windows service control is only supported on Windows."

        if cls.uses_synctrayzor(service_name):
            return SyncTrayzorController.stop()

        current = cls.get_raw_service_status(service_name)
        if current == "n/a":
            return False, f"Service '{service_name}' is not installed."
        if current == "stopped":
            return True, f"Service '{service_name}' is already stopped."

        scm = _advapi32.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not scm:
            return False, f"Unable to open Service Control Manager (error {ctypes.get_last_error()})."
        try:
            svc = _advapi32.OpenServiceW(scm, service_name, SERVICE_STOP | SERVICE_QUERY_STATUS)
            if not svc:
                err = ctypes.get_last_error()
                if err == ERROR_ACCESS_DENIED:
                    return cls._elevated_sc("stop", service_name, "stopped")
                if err == ERROR_SERVICE_DOES_NOT_EXIST:
                    return False, f"Service '{service_name}' is not installed."
                return False, f"Failed to open service '{service_name}' (error {err})."
            try:
                status = SERVICE_STATUS()
                if not _advapi32.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(status)):
                    err = ctypes.get_last_error()
                    if err == ERROR_SERVICE_NOT_ACTIVE:
                        return True, f"Service '{service_name}' is already stopped."
                    if err == ERROR_ACCESS_DENIED:
                        return cls._elevated_sc("stop", service_name, "stopped")
                    return False, f"Failed to stop '{service_name}' (error {err})."
            finally:
                _advapi32.CloseServiceHandle(svc)
        finally:
            _advapi32.CloseServiceHandle(scm)

        if cls._wait_for_state(service_name, "stopped", timeout=20.0):
            return True, f"Service '{service_name}' stopped."
        return False, f"Timed out waiting for '{service_name}' to stop."

    @staticmethod
    def get_current_user_sid() -> tuple[str, str]:
        """Return (username, sid) for the currently logged-in Windows user."""
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        res = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            creationflags=flags,
            check=True,
        )
        reader = csv.reader(io.StringIO(res.stdout.strip()))
        row = next(reader, None)
        if not row or len(row) < 2 or not row[1].startswith("S-"):
            raise RuntimeError(f"Unable to parse current user SID from whoami output: {res.stdout!r}")
        return row[0].strip(), row[1].strip()

    @classmethod
    def grant_current_user_permissions(
        cls, services: tuple[str, ...] = DEFAULT_SERVICES
    ) -> tuple[bool, str]:
        """
        Elevate once via UAC and grant the current Windows user start/stop/query
        permissions on any installed services in `services`.
        """
        if sys.platform != "win32":
            return False, "Windows service permissions are only supported on Windows."

        installed = [svc for svc in services if cls.get_raw_service_status(svc) != "n/a"]
        missing = [svc for svc in services if svc not in installed]
        extra_notes: list[str] = []
        for svc in list(missing):
            if cls.uses_synctrayzor(svc):
                missing.remove(svc)
                extra_notes.append(
                    "  - SyncTrayzor: User-mode app (no Admin service permission needed)."
                )

        if not installed:
            lines = ["No Windows services require Admin permission changes:"]
            lines.extend(extra_notes)
            lines.extend(f"  - {s}: Not installed as a Windows service." for s in missing)
            return True, "\n".join(lines)

        try:
            username, user_sid = cls.get_current_user_sid()
        except Exception as exc:
            return False, f"Could not determine current user SID:\n{exc}"

        temp_dir = Path(tempfile.gettempdir())
        stamp = f"{os.getpid()}_{int(time.time() * 1000)}"
        script_path = temp_dir / f"wmm_grant_svc_{stamp}.ps1"
        log_path = temp_dir / f"wmm_grant_svc_{stamp}.log"

        svc_list_ps = ", ".join(f"'{s}'" for s in installed)
        ps_script = f"""$ErrorActionPreference = 'Stop'
$userSid = '{user_sid}'
$ace = "(A;;CCLCRPWPLO;;;$userSid)"
$services = @({svc_list_ps})
$logPath = '{str(log_path).replace("'", "''")}'
$lines = New-Object System.Collections.Generic.List[string]
$allOk = $true

foreach ($svc in $services) {{
    try {{
        $raw = (& sc.exe sdshow $svc 2>&1) -join "`n"
        if ($LASTEXITCODE -ne 0) {{
            $lines.Add("FAIL|$svc|sc sdshow failed: $($raw.Trim())")
            $allOk = $false
            continue
        }}
        $sddl = ($raw -split "`r?`n" | ForEach-Object {{ $_.Trim() }} | Where-Object {{ $_ -like 'D:*' }} | Select-Object -First 1)
        if (-not $sddl) {{
            $lines.Add("FAIL|$svc|Could not parse DACL from: $($raw.Trim())")
            $allOk = $false
            continue
        }}

        $escapedSid = [regex]::Escape($userSid)
        if ($sddl -match "\\(A;;[^;]*RP[^;]*WP[^;]*;;;$escapedSid\\)" -or $sddl -match "\\(A;;[^;]*WP[^;]*RP[^;]*;;;$escapedSid\\)") {{
            $lines.Add("OK|$svc|Already has start/stop permission.")
            continue
        }}

        $saclIdx = $sddl.IndexOf('S:')
        if ($saclIdx -ge 0) {{
            $newSddl = $sddl.Substring(0, $saclIdx) + $ace + $sddl.Substring($saclIdx)
        }} else {{
            $newSddl = $sddl + $ace
        }}

        $setOut = (& sc.exe sdset $svc $newSddl 2>&1) -join ' '
        if ($LASTEXITCODE -eq 0) {{
            $lines.Add("OK|$svc|Granted start/stop permission.")
        }} else {{
            $lines.Add("FAIL|$svc|sc sdset failed: $($setOut.Trim())")
            $allOk = $false
        }}
    }} catch {{
        $lines.Add("FAIL|$svc|$($_.Exception.Message)")
        $allOk = $false
    }}
}}

$lines | Set-Content -Path $logPath -Encoding UTF8
if (-not $allOk) {{ exit 1 }}
exit 0
"""

        try:
            script_path.write_text(ps_script, encoding="utf-8")

            sei = SHELLEXECUTEINFOW()
            sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
            sei.fMask = SEE_MASK_NOCLOSEPROCESS
            sei.hwnd = None
            sei.lpVerb = "runas"
            sei.lpFile = "powershell.exe"
            sei.lpParameters = f'-NoProfile -ExecutionPolicy Bypass -File "{script_path}"'
            sei.lpDirectory = None
            sei.nShow = 0  # SW_HIDE

            if not _shell32.ShellExecuteExW(ctypes.byref(sei)):
                err = ctypes.get_last_error()
                return False, f"Administrator elevation was cancelled or failed (error {err})."

            if sei.hProcess:
                try:
                    _kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
                finally:
                    _kernel32.CloseHandle(sei.hProcess)

            summary_lines = [f"User: {username} ({user_sid})", ""]
            all_ok = True
            if log_path.is_file():
                for line in log_path.read_text(encoding="utf-8-sig").splitlines():
                    parts = line.split("|", 2)
                    if len(parts) == 3:
                        status_tag, svc_name, detail = parts
                        if status_tag != "OK":
                            all_ok = False
                        summary_lines.append(f"  - {svc_name}: {detail}")
                    elif line.strip():
                        summary_lines.append(f"  - {line.strip()}")
            else:
                all_ok = False
                summary_lines.append("  - Elevated helper did not produce a result log.")

            summary_lines.extend(extra_notes)
            for svc_name in missing:
                summary_lines.append(f"  - {svc_name}: Not installed (skipped).")

            return all_ok, "\n".join(summary_lines)
        finally:
            for p in (script_path, log_path):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Configuration Management
# ---------------------------------------------------------------------------

def is_divider_item(item: dict[str, str]) -> bool:
    """Return True if a menu config entry represents a visual horizontal divider."""
    return str(item.get("type", "")).lower() == "divider"


class StartupManager:
    """Manage per-user Windows login startup via HKCU\\...\\CurrentVersion\\Run."""

    RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

    @staticmethod
    def _value_name(config_path: Path) -> str:
        if config_path.name.lower() == DEFAULT_CONFIG_FILENAME.lower():
            return "WMM-Menu"
        return f"WMM-Menu ({config_path.stem})"

    @staticmethod
    def _build_command(config_path: Path) -> str:
        cfg_abs = str(config_path.resolve())
        if getattr(sys, "frozen", False):
            exe_abs = str(Path(sys.executable).resolve())
            return f'"{exe_abs}" --config "{cfg_abs}"'
        py_exe = Path(sys.executable).resolve()
        pyw_exe = py_exe.with_name("pythonw.exe")
        runner = pyw_exe if pyw_exe.is_file() else py_exe
        script_abs = str(Path(__file__).resolve())
        return f'"{runner}" "{script_abs}" --config "{cfg_abs}"'

    @classmethod
    def is_enabled(cls, config_path: Path) -> bool:
        if sys.platform != "win32":
            return False
        name = cls._value_name(config_path)
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, cls.RUN_KEY_PATH, 0, winreg.KEY_READ
            ) as key:
                val, _ = winreg.QueryValueEx(key, name)
                return bool(str(val).strip())
        except OSError:
            return False

    @classmethod
    def set_enabled(cls, config_path: Path, enabled: bool) -> None:
        if sys.platform != "win32":
            return
        name = cls._value_name(config_path)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, cls.RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, name, 0, winreg.REG_SZ, cls._build_command(config_path)
                )
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass


class ConfigManager:
    """Loads and saves YAML configuration for wmm-menu."""

    def __init__(self, config_path: Path):
        self.path = config_path
        self.title: str = APP_TITLE
        self.base_folder: str = DEFAULT_BASE_FOLDER
        self.backup_folder: str = str(Path(DEFAULT_BASE_FOLDER) / "backups")
        self.backup_exclude_caches: bool = True
        self.items: list[dict[str, str]] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.title = APP_TITLE
            self.base_folder = DEFAULT_BASE_FOLDER
            self.backup_folder = str(Path(DEFAULT_BASE_FOLDER) / "backups")
            self.backup_exclude_caches = True
            self.items = []
            self.save()
            return

        with self.path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        if not isinstance(data, dict):
            data = {}

        self.title = str(data.get("title") or APP_TITLE)
        self.base_folder = str(data.get("base_folder") or DEFAULT_BASE_FOLDER)
        raw_backup = data.get("backup_folder")
        if raw_backup:
            self.backup_folder = str(raw_backup)
        else:
            self.backup_folder = str(Path(self.base_folder) / "backups")

        raw_exclude = data.get("backup_exclude_caches")
        self.backup_exclude_caches = True if raw_exclude is None else bool(raw_exclude)

        raw_items = data.get("items") or []
        self.items = []
        if isinstance(raw_items, list):
            for entry in raw_items:
                if not isinstance(entry, dict):
                    continue
                if is_divider_item(entry):
                    self.items.append({"type": "divider"})
                elif entry.get("name") and entry.get("subfolder"):
                    self.items.append(
                        {
                            "name": str(entry["name"]),
                            "subfolder": str(entry["subfolder"]),
                        }
                    )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "title": self.title,
            "base_folder": self.base_folder,
            "backup_folder": self.backup_folder,
            "backup_exclude_caches": self.backup_exclude_caches,
            "items": self.items,
        }
        with self.path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Pure-Python Config Backup
# ---------------------------------------------------------------------------

# Volatile directories excluded when backup_exclude_caches is enabled
VOLATILE_TOP_DIRS = {
    "logs",
    "crashes",
    "profiler_data",
    "updates",
}
VOLATILE_BROWSER_CACHE_DIRS = {
    "cache",
    "code cache",
    "gpucache",
    "dawnwebgpucache",
    "dawngraphitecache",
    "shadercache",
    "grshadercache",
    "blob_storage",
    "service worker",
    "indexeddb",
    "session storage",
    "shared dictionary",
    "crashpad",
}


def backup_obs_config(
    obs_folder: Path,
    backup_dir: Path,
    progress_cb: Callable[[int, int, str], None] | None = None,
    exclude_caches: bool = True,
) -> Path:
    """
    Create a ZIP archive of <obs_folder>/config in <backup_dir>.

    Archive filename: <obs_folder.name>-<YYYYMMDD_HHMMSS>.zip (24-hour clock).
    """
    config_dir = obs_folder / "config"
    if not config_dir.is_dir():
        raise FileNotFoundError(f"OBS config folder not found: {config_dir}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = backup_dir / f"{obs_folder.name}-{timestamp}.zip"

    files_to_zip: list[tuple[Path, str]] = []
    for root, dirs, filenames in os.walk(config_dir):
        root_path = Path(root)
        if exclude_caches:
            rel_parts = [p.lower() for p in root_path.relative_to(config_dir).parts]
            in_browser = "obs-browser" in rel_parts
            dirs[:] = [
                d
                for d in dirs
                if d.lower() not in VOLATILE_TOP_DIRS
                and not (in_browser and d.lower() in VOLATILE_BROWSER_CACHE_DIRS)
            ]
        for fname in filenames:
            full_path = root_path / fname
            arcname = str(full_path.relative_to(config_dir))
            files_to_zip.append((full_path, arcname))

    total = len(files_to_zip)
    if progress_cb:
        progress_cb(0, max(total, 1), "Preparing archive...")

    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        strict_timestamps=False,
    ) as zf:
        for idx, (full_path, arcname) in enumerate(files_to_zip, start=1):
            if progress_cb:
                progress_cb(idx - 1, max(total, 1), arcname)
            zf.write(full_path, arcname=arcname)
            if progress_cb:
                progress_cb(idx, max(total, 1), arcname)

    if total == 0 and progress_cb:
        progress_cb(1, 1, "Config folder was empty.")

    return archive_path


# ---------------------------------------------------------------------------
# Executable ProductVersion Reader & Lightning Bolt Icon Builder
# ---------------------------------------------------------------------------

class VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


_EXE_VERSION_CACHE: dict[tuple[str, int], str | None] = {}


def get_exe_product_version(exe_path: Path) -> str | None:
    """Read the Windows PE 'ProductVersion' string from an executable."""
    if sys.platform != "win32":
        return None
    try:
        st = exe_path.stat()
    except OSError:
        return None

    cache_key = (str(exe_path).lower(), st.st_mtime_ns)
    if cache_key in _EXE_VERSION_CACHE:
        return _EXE_VERSION_CACHE[cache_key]

    path_str = str(exe_path)
    dummy = wintypes.DWORD(0)
    size = _version_dll.GetFileVersionInfoSizeW(path_str, ctypes.byref(dummy))
    if not size:
        _EXE_VERSION_CACHE[cache_key] = None
        return None

    buf = ctypes.create_string_buffer(size)
    if not _version_dll.GetFileVersionInfoW(path_str, 0, size, buf):
        _EXE_VERSION_CACHE[cache_key] = None
        return None

    version_str: str | None = None

    # 1. Query StringFileInfo\<lang><codepage>\ProductVersion
    trans_ptr = ctypes.c_void_p()
    trans_len = wintypes.UINT(0)
    lang_codepages: list[str] = []
    if (
        _version_dll.VerQueryValueW(
            buf, r"\VarFileInfo\Translation", ctypes.byref(trans_ptr), ctypes.byref(trans_len)
        )
        and trans_ptr.value
        and trans_len.value >= 4
    ):
        count = trans_len.value // 4
        words = (wintypes.WORD * (count * 2)).from_address(trans_ptr.value)
        for i in range(count):
            lang_codepages.append(f"{words[2 * i]:04x}{words[2 * i + 1]:04x}")

    for fallback in ("040904b0", "040904e4", "000004b0"):
        if fallback not in lang_codepages:
            lang_codepages.append(fallback)

    for lc in lang_codepages:
        val_ptr = ctypes.c_void_p()
        val_len = wintypes.UINT(0)
        sub_block = f"\\StringFileInfo\\{lc}\\ProductVersion"
        if (
            _version_dll.VerQueryValueW(
                buf, sub_block, ctypes.byref(val_ptr), ctypes.byref(val_len)
            )
            and val_ptr.value
            and val_len.value > 0
        ):
            raw = ctypes.wstring_at(val_ptr.value).strip()
            if raw:
                version_str = raw
                break

    # 2. Fallback to VS_FIXEDFILEINFO dwProductVersionMS/LS
    if not version_str:
        ffi_ptr = ctypes.c_void_p()
        ffi_len = wintypes.UINT(0)
        if (
            _version_dll.VerQueryValueW(
                buf, "\\", ctypes.byref(ffi_ptr), ctypes.byref(ffi_len)
            )
            and ffi_ptr.value
            and ffi_len.value >= ctypes.sizeof(VS_FIXEDFILEINFO)
        ):
            ffi = VS_FIXEDFILEINFO.from_address(ffi_ptr.value)
            major = (ffi.dwProductVersionMS >> 16) & 0xFFFF
            minor = ffi.dwProductVersionMS & 0xFFFF
            build = (ffi.dwProductVersionLS >> 16) & 0xFFFF
            rev = ffi.dwProductVersionLS & 0xFFFF
            version_str = (
                f"{major}.{minor}.{build}.{rev}" if rev else f"{major}.{minor}.{build}"
            )

    _EXE_VERSION_CACHE[cache_key] = version_str
    return version_str


def _draw_bolt_pil(fill: str, outline: str | None = None, outline_width: int = 2) -> Image.Image:
    """Render a supersampled 64x64 lightning bolt RGBA image."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    points = [(37, 3), (13, 35), (30, 35), (24, 61), (51, 27), (34, 27)]
    draw.polygon(points, fill=fill)
    if outline:
        draw.line([*points, points[0]], fill=outline, width=outline_width, joint="curve")
    return img


def build_lightning_icons() -> tuple[ctk.CTkImage, ctk.CTkImage]:
    """Return (active_yellow_bolt, disabled_gray_bolt) CTkImage instances."""
    active_light = _draw_bolt_pil(fill="#fde047", outline="#b45309", outline_width=2)
    active_dark = _draw_bolt_pil(fill="#fde047", outline="#ca8a04", outline_width=2)
    disabled_light = _draw_bolt_pil(fill="#9ca3af", outline=None)
    disabled_dark = _draw_bolt_pil(fill="#6e7681", outline=None)
    return (
        ctk.CTkImage(light_image=active_light, dark_image=active_dark, size=(20, 20)),
        ctk.CTkImage(light_image=disabled_light, dark_image=disabled_dark, size=(20, 20)),
    )


def _draw_gear_pil(fill: str) -> Image.Image:
    """Render a supersampled 128x128 8-tooth gear RGBA image and Lanczos-downsample to 64x64."""
    size = 128
    cx = cy = size / 2.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer gear body + 8 trapezoidal teeth
    num_teeth = 8
    r_base = 39.0
    r_outer = 56.0
    tooth_half_base = math.radians(13.5)
    tooth_half_tip = math.radians(8.5)

    poly: list[tuple[float, float]] = []
    for i in range(num_teeth):
        a = i * (2.0 * math.pi / num_teeth)
        a_next = (i + 1) * (2.0 * math.pi / num_teeth)
        poly.append((cx + r_base * math.cos(a - tooth_half_base), cy + r_base * math.sin(a - tooth_half_base)))
        poly.append((cx + r_outer * math.cos(a - tooth_half_tip), cy + r_outer * math.sin(a - tooth_half_tip)))
        poly.append((cx + r_outer * math.cos(a + tooth_half_tip), cy + r_outer * math.sin(a + tooth_half_tip)))
        poly.append((cx + r_base * math.cos(a + tooth_half_base), cy + r_base * math.sin(a + tooth_half_base)))
        # Arc bridge along r_base to next tooth
        mid = 0.5 * ((a + tooth_half_base) + (a_next - tooth_half_base))
        poly.append((cx + r_base * math.cos(mid), cy + r_base * math.sin(mid)))

    draw.polygon(poly, fill=fill)
    draw.ellipse((cx - 42, cy - 42, cx + 42, cy + 42), fill=fill)
    # Transparent center hole
    draw.ellipse((cx - 19, cy - 19, cx + 19, cy + 19), fill=(0, 0, 0, 0))
    return img.resize((64, 64), Image.Resampling.LANCZOS)


def build_gear_icon() -> ctk.CTkImage:
    """Return a crisp light/dark gear CTkImage."""
    gear_light = _draw_gear_pil("#4b5563")
    gear_dark = _draw_gear_pil("#c9d1d9")
    return ctk.CTkImage(light_image=gear_light, dark_image=gear_dark, size=(16, 16))


# ---------------------------------------------------------------------------
# Modern Light/Dark Color Tokens, Sizing & Hover ToolTip
# ---------------------------------------------------------------------------

BTN_HEIGHT = 24
MENU_BTN_HEIGHT = 48  # Doubled height for main OBS launch buttons

COLOR_TEXT_MUTED = ("#57606a", "#8b949e")
COLOR_CARD_BG = ("#f6f8fa", "#1c2128")
COLOR_CARD_BORDER = ("#d0d7de", "#30363d")
COLOR_DIVIDER = ("#d0d7de", "#30363d")

# Neutral secondary buttons (dialogs, gear button)
COLOR_MAIN_BTN_BG = ("#eef2f7", "#2d333b")
COLOR_MAIN_BTN_HOVER = ("#dce6f4", "#3b434d")
COLOR_MAIN_BTN_TEXT = ("#1f2328", "#f0f6fc")
COLOR_BTN_DISABLED_BG = ("#d8dee4", "#22272e")
COLOR_BTN_DISABLED_TEXT = ("#8c959f", "#6e7681")

COLOR_FAST_BTN_BG = ("#e6ebf2", "#343b44")
COLOR_FAST_BTN_HOVER = ("#dce3ec", "#424a55")

# Prominent high-contrast colors for main OBS menu item buttons
COLOR_MENU_ROW_BORDER = ("#154d91", "#388bfd")
COLOR_MENU_BTN_BG = ("#1d63b8", "#1f6aa5")
COLOR_MENU_BTN_HOVER = ("#154d91", "#2b7ec2")
COLOR_MENU_BTN_TEXT = ("#ffffff", "#ffffff")
COLOR_MENU_FAST_BG = ("#154886", "#164270")
COLOR_MENU_FAST_HOVER = ("#1e5ca8", "#235a94")

COLOR_ACCENT_WARN_BG = ("#fae7c3", "#4e3914")
COLOR_ACCENT_WARN_HOVER = ("#f3d499", "#694d1b")
COLOR_ACCENT_WARN_TEXT = ("#5c3d00", "#ffd98a")


class ToolTip:
    """Lightweight Light/Dark-aware hover tooltip for CustomTkinter widgets."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 250):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._tip_win: tk.Toplevel | None = None

        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _on_enter(self, _event: tk.Event) -> None:
        if self._tip_win is not None:
            return
        self._cancel_timer()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _on_leave(self, _event: tk.Event) -> None:
        # Ignore synthetic <Leave> when moving onto an internal child label/image of CTkButton
        if self.widget.winfo_exists():
            px, py = self.widget.winfo_pointerxy()
            wx, wy = self.widget.winfo_rootx(), self.widget.winfo_rooty()
            ww, wh = self.widget.winfo_width(), self.widget.winfo_height()
            if wx <= px < wx + ww and wy <= py < wy + wh:
                return
        self._hide()

    def _cancel_timer(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    @staticmethod
    def _get_display_scale_and_bounds(
        px: int, py: int, widget: tk.Misc
    ) -> tuple[float, tuple[int, int, int, int]]:
        """Return (dpi_scale, (left, top, right, bottom)) for the monitor at (px, py)."""
        ctk_scale = float(getattr(widget, "_get_widget_scaling", lambda: 1.0)() or 1.0)
        dpi_scale = ctk_scale
        bounds = (0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight())

        if sys.platform == "win32":
            hmon = _user32.MonitorFromPoint(wintypes.POINT(px, py), MONITOR_DEFAULTTONEAREST)
            if hmon:
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    bounds = (
                        mi.rcWork.left,
                        mi.rcWork.top,
                        mi.rcWork.right,
                        mi.rcWork.bottom,
                    )
                if _shcore is not None:
                    xdpi = wintypes.UINT(96)
                    ydpi = wintypes.UINT(96)
                    if (
                        _shcore.GetDpiForMonitor(
                            hmon, MDT_EFFECTIVE_DPI, ctypes.byref(xdpi), ctypes.byref(ydpi)
                        )
                        == 0
                        and xdpi.value > 0
                    ):
                        dpi_scale = max(dpi_scale, (xdpi.value + ydpi.value) / 192.0)

            # Also respect Windows Accessibility 'Text size' slider if set > 100%
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Accessibility",
                    0,
                    winreg.KEY_READ,
                ) as key:
                    tsf, _ = winreg.QueryValueEx(key, "TextScaleFactor")
                    text_factor = float(tsf) / 100.0
                    if text_factor > 1.0:
                        dpi_scale *= text_factor
            except OSError:
                pass

        return max(1.0, dpi_scale), bounds

    def _show(self) -> None:
        self._after_id = None
        if not self.widget.winfo_exists() or self._tip_win is not None:
            return

        is_dark = ctk.get_appearance_mode().lower() == "dark"
        bg_border = "#388bfd" if is_dark else "#8c959f"
        bg_inner = "#161b22" if is_dark else "#1f2328"
        fg_text = "#f0f6fc" if is_dark else "#ffffff"

        px, py = self.widget.winfo_pointerxy()
        scale, (m_left, m_top, m_right, m_bottom) = self._get_display_scale_and_bounds(
            px, py, self.widget
        )

        font_px = -max(12, round(12 * scale))
        pad_x = max(6, round(10 * scale))
        pad_y = max(3, round(5 * scale))
        border_px = max(1, round(1 * scale))
        off_x = round(12 * scale)
        off_y = round(20 * scale)
        margin = round(8 * scale)

        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except tk.TclError:
            pass

        border = tk.Frame(tw, bg=bg_border, bd=0)
        border.pack(fill=tk.BOTH, expand=True)
        lbl = tk.Label(
            border,
            text=self.text,
            bg=bg_inner,
            fg=fg_text,
            font=("Segoe UI", font_px),
            padx=pad_x,
            pady=pad_y,
        )
        lbl.pack(padx=border_px, pady=border_px)

        tw.update_idletasks()
        tip_w = tw.winfo_reqwidth()
        tip_h = tw.winfo_reqheight()

        x = min(max(m_left + 4, px + off_x), max(m_left + 4, m_right - tip_w - margin))
        y = py + off_y
        if y + tip_h > m_bottom - margin:
            y = max(m_top + 4, py - tip_h - margin)

        tw.wm_geometry(f"+{x}+{y}")
        self._tip_win = tw

    def _hide(self, _event: tk.Event | None = None) -> None:
        self._cancel_timer()
        if self._tip_win is not None:
            try:
                if self._tip_win.winfo_exists():
                    self._tip_win.destroy()
            except tk.TclError:
                pass
            self._tip_win = None

COLOR_SVC_RUN_BG = ("#dafbe1", "#132e1e")
COLOR_SVC_RUN_HOVER = ("#c2f0cc", "#1b432b")
COLOR_SVC_RUN_BORDER = ("#82cfa0", "#2ea043")
COLOR_SVC_RUN_TEXT = ("#116329", "#56d364")

COLOR_SVC_STOP_BG = ("#ffebe9", "#361618")
COLOR_SVC_STOP_HOVER = ("#ffd7d5", "#4c1f22")
COLOR_SVC_STOP_BORDER = ("#ffaba8", "#f85149")
COLOR_SVC_STOP_TEXT = ("#cf222e", "#ff7b72")

COLOR_SVC_PEND_BG = ("#fff8c5", "#362a09")
COLOR_SVC_PEND_BORDER = ("#e3c15b", "#d29922")
COLOR_SVC_PEND_TEXT = ("#7d4e00", "#e3b341")

COLOR_SVC_NA_BG = ("#eaeef2", "#21262d")
COLOR_SVC_NA_BORDER = ("#d0d7de", "#30363d")
COLOR_SVC_NA_TEXT = ("#6e7781", "#6e7681")


def get_primary_work_area(fallback_widget: tk.Misc) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) in physical pixels for the main screen work area."""
    if sys.platform == "win32":
        rect = wintypes.RECT()
        if _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    return 0, 0, fallback_widget.winfo_screenwidth(), fallback_widget.winfo_screenheight()


# ---------------------------------------------------------------------------
# Launch Progress Popup Window
# ---------------------------------------------------------------------------

class LaunchProgressWindow(ctk.CTkToplevel):
    """Per-launch popup showing service stopping, backup progress, and OBS status."""

    def __init__(self, master: tk.Misc, item_name: str, subfolder: str, fast: bool):
        super().__init__(master)
        mode_tag = " (Fast)" if fast else ""
        self.title(f"OBS - {item_name}{mode_tag}")
        wa_left, wa_top, _wa_right, _wa_bottom = get_primary_work_area(master)
        self._pos_x = wa_left + 8
        self._pos_y = wa_top + 8
        self.geometry(f"480x290+{self._pos_x}+{self._pos_y}")
        self.resizable(False, False)
        apply_window_icon(self)
        self._can_close = False
        self._is_indeterminate = True

        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)

        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill=tk.BOTH, expand=True, padx=16, pady=14)

        ctk.CTkLabel(
            container,
            text=f"{item_name}{mode_tag}",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            anchor="w",
        ).pack(fill=tk.X)

        ctk.CTkLabel(
            container,
            text=f"Folder: {subfolder}",
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 10))

        self.step_var = tk.StringVar(value="Initializing...")
        ctk.CTkLabel(
            container,
            textvariable=self.step_var,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 6))

        self.progress = ctk.CTkProgressBar(container, mode="indeterminate", height=10)
        self.progress.pack(fill=tk.X, pady=(0, 4))
        self.progress.start()

        self.detail_var = tk.StringVar(value="")
        ctk.CTkLabel(
            container,
            textvariable=self.detail_var,
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 8))

        self.log_text = ctk.CTkTextbox(
            container,
            height=105,
            wrap="word",
            state="disabled",
            font=ctk.CTkFont(family="Consolas", size=11),
            corner_radius=6,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.close_btn = ctk.CTkButton(
            container,
            text="Close",
            width=84,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            command=self.destroy,
            state="disabled",
        )
        self.close_btn.pack(anchor="e", pady=(10, 0))

    def _on_close_attempt(self) -> None:
        if self._can_close:
            self.destroy()
        else:
            self.iconify()

    def set_step(self, text: str, indeterminate: bool = True) -> None:
        self.step_var.set(text)
        if indeterminate:
            if not self._is_indeterminate:
                self.progress.configure(mode="indeterminate")
                self.progress.start()
                self._is_indeterminate = True
        else:
            if self._is_indeterminate:
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self._is_indeterminate = False
            self.progress.set(0.0)

    def set_progress(self, current: int, total: int, detail: str) -> None:
        if self._is_indeterminate:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._is_indeterminate = False
        ratio = (current / total) if total > 0 else 1.0
        self.progress.set(max(0.0, min(1.0, ratio)))
        pct = ratio * 100.0
        self.detail_var.set(f"{pct:5.1f}% ({current}/{total})  {detail}")

    def set_detail(self, text: str) -> None:
        self.detail_var.set(text)

    def append_log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"[{ts}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def mark_finished(self, auto_close: bool = True) -> None:
        self._can_close = True
        if self._is_indeterminate:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self._is_indeterminate = False
        self.progress.set(1.0)
        self.close_btn.configure(state="normal")
        if auto_close:
            self.after(1200, self._safe_destroy)

    def _safe_destroy(self) -> None:
        if self.winfo_exists():
            self.destroy()


# ---------------------------------------------------------------------------
# Dialogs: Config & Add/Edit Item
# ---------------------------------------------------------------------------

class ConfigDialog(ctk.CTkToplevel):
    """Dialog to edit menu title, folders, backup options, and button order."""

    def __init__(self, master: tk.Misc, config: ConfigManager, on_saved: Callable[[], None]):
        super().__init__(master)
        self.config_mgr = config
        self.on_saved = on_saved
        self._items_draft: list[dict[str, str]] = [dict(it) for it in config.items]

        self.title("Configuration")
        self.geometry("580x595")
        self.minsize(520, 510)
        self.resizable(True, True)
        apply_window_icon(self)
        self.transient(master)
        self.grab_set()

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=14)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)

        ctk.CTkLabel(
            frame,
            text="Config File:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        ctk.CTkLabel(
            frame,
            text=str(config.path),
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            anchor="w",
        ).grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(0, 8))

        ctk.CTkLabel(
            frame,
            text="Title:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        ).grid(row=1, column=0, sticky="w", pady=4)
        self.title_var = tk.StringVar(value=config.title)
        self.title_entry = ctk.CTkEntry(frame, textvariable=self.title_var, height=28)
        self.title_entry.grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4
        )

        ctk.CTkLabel(frame, text="Base Folder:").grid(row=2, column=0, sticky="w", pady=4)
        self.base_var = tk.StringVar(value=config.base_folder)
        ctk.CTkEntry(frame, textvariable=self.base_var, height=28).grid(
            row=2, column=1, sticky="ew", padx=(8, 8), pady=4
        )
        ctk.CTkButton(
            frame,
            text="Browse...",
            width=80,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLOR_MAIN_BTN_BG,
            hover_color=COLOR_MAIN_BTN_HOVER,
            text_color=COLOR_MAIN_BTN_TEXT,
            command=self._browse_base,
        ).grid(row=2, column=2, pady=4)

        ctk.CTkLabel(frame, text="Backup Folder:").grid(row=3, column=0, sticky="w", pady=4)
        self.backup_var = tk.StringVar(value=config.backup_folder)
        ctk.CTkEntry(frame, textvariable=self.backup_var, height=28).grid(
            row=3, column=1, sticky="ew", padx=(8, 8), pady=4
        )
        ctk.CTkButton(
            frame,
            text="Browse...",
            width=80,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLOR_MAIN_BTN_BG,
            hover_color=COLOR_MAIN_BTN_HOVER,
            text_color=COLOR_MAIN_BTN_TEXT,
            command=self._browse_backup,
        ).grid(row=3, column=2, pady=4)

        ctk.CTkButton(
            frame,
            text="Use <Base Folder>\\backups",
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_MAIN_BTN_TEXT,
            hover_color=COLOR_MAIN_BTN_HOVER,
            command=self._reset_backup_default,
        ).grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(2, 6))

        self.exclude_caches_var = tk.BooleanVar(value=config.backup_exclude_caches)
        ctk.CTkCheckBox(
            frame,
            text="Exclude volatile caches & logs from backups (obs-browser cache, logs, crashes)",
            variable=self.exclude_caches_var,
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 4))

        self.run_at_login_var = tk.BooleanVar(value=StartupManager.is_enabled(config.path))
        ctk.CTkCheckBox(
            frame,
            text="Run this menu automatically when user logs in",
            variable=self.run_at_login_var,
            font=ctk.CTkFont(family="Segoe UI", size=12),
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 8))

        # Menu Buttons Section (Add / Divider / Edit / Remove / Reorder)
        order_hdr = ctk.CTkFrame(frame, fg_color="transparent")
        order_hdr.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(2, 4))
        order_hdr.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            order_hdr,
            text="Menu Buttons:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            order_hdr,
            text="+ Divider",
            width=78,
            height=BTN_HEIGHT,
            border_spacing=1,
            corner_radius=5,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLOR_MAIN_BTN_BG,
            hover_color=COLOR_MAIN_BTN_HOVER,
            text_color=COLOR_MAIN_BTN_TEXT,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._add_divider_draft,
        ).grid(row=0, column=1, sticky="e", padx=(0, 6))

        ctk.CTkButton(
            order_hdr,
            text="+ Add",
            width=72,
            height=BTN_HEIGHT,
            border_spacing=1,
            corner_radius=5,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            command=self._open_add_item,
        ).grid(row=0, column=2, sticky="e")

        self.order_list = ctk.CTkScrollableFrame(
            frame,
            height=140,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=8,
        )
        self.order_list.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(0, 10))
        self._render_order_list()

        ctk.CTkFrame(frame, height=1, fg_color=COLOR_DIVIDER).grid(
            row=9, column=0, columnspan=3, sticky="ew", pady=(2, 10)
        )

        perm_row = ctk.CTkFrame(frame, fg_color="transparent")
        perm_row.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.grant_btn = ctk.CTkButton(
            perm_row,
            text="Grant Start/Stop Service Permissions (Admin)",
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLOR_ACCENT_WARN_BG,
            hover_color=COLOR_ACCENT_WARN_HOVER,
            text_color=COLOR_ACCENT_WARN_TEXT,
            command=self._grant_service_permissions,
        )
        self.grant_btn.pack(side=tk.LEFT)
        ctk.CTkLabel(
            perm_row,
            text="for syncthing & rslsyncsvc",
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
        ).pack(side=tk.LEFT, padx=(10, 0))

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.grid(row=11, column=0, columnspan=3, sticky="e", pady=(4, 0))
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=84,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_MAIN_BTN_TEXT,
            hover_color=COLOR_MAIN_BTN_HOVER,
            command=self.destroy,
        ).pack(side=tk.RIGHT, padx=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="Save",
            width=84,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            command=self._save,
        ).pack(side=tk.RIGHT)

    def _render_order_list(self) -> None:
        for child in self.order_list.winfo_children():
            child.destroy()

        if not self._items_draft:
            ctk.CTkLabel(
                self.order_list,
                text="No menu buttons configured. Click '+ Add' above to add one.",
                text_color=COLOR_TEXT_MUTED,
                font=ctk.CTkFont(family="Segoe UI", size=11),
            ).pack(pady=16)
            return

        total = len(self._items_draft)
        for idx, item in enumerate(self._items_draft):
            divider_entry = is_divider_item(item)
            row = ctk.CTkFrame(
                self.order_list,
                height=BTN_HEIGHT + 2,
                corner_radius=5,
                fg_color=COLOR_CARD_BG if divider_entry else COLOR_MAIN_BTN_BG,
                border_width=1 if divider_entry else 0,
                border_color=COLOR_CARD_BORDER,
            )
            row.pack(fill=tk.X, pady=2, padx=2)
            row.columnconfigure(0, weight=1)
            for col in (1, 2, 3, 4):
                row.columnconfigure(col, weight=0)

            label_text = (
                f"  {idx + 1}.   ────────  Divider  ────────"
                if divider_entry
                else f"  {idx + 1}.  {item['name']}   ({item['subfolder']})"
            )
            ctk.CTkLabel(
                row,
                text=label_text,
                height=BTN_HEIGHT,
                anchor="w",
                text_color=COLOR_TEXT_MUTED if divider_entry else COLOR_MAIN_BTN_TEXT,
                font=ctk.CTkFont(family="Segoe UI", size=12),
            ).grid(row=0, column=0, sticky="ew", padx=(4, 6), pady=1)

            up_disabled = idx == 0
            ctk.CTkButton(
                row,
                text="▲",
                width=26,
                height=BTN_HEIGHT - 2,
                border_spacing=0,
                corner_radius=4,
                font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                fg_color=COLOR_BTN_DISABLED_BG if up_disabled else COLOR_FAST_BTN_BG,
                hover_color=COLOR_MAIN_BTN_HOVER,
                text_color=COLOR_MAIN_BTN_TEXT,
                text_color_disabled=COLOR_BTN_DISABLED_TEXT,
                state="disabled" if up_disabled else "normal",
                command=lambda i=idx: self._move_item(i, -1),
            ).grid(row=0, column=1, padx=(0, 3), pady=2)

            dn_disabled = idx == total - 1
            ctk.CTkButton(
                row,
                text="▼",
                width=26,
                height=BTN_HEIGHT - 2,
                border_spacing=0,
                corner_radius=4,
                font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                fg_color=COLOR_BTN_DISABLED_BG if dn_disabled else COLOR_FAST_BTN_BG,
                hover_color=COLOR_MAIN_BTN_HOVER,
                text_color=COLOR_MAIN_BTN_TEXT,
                text_color_disabled=COLOR_BTN_DISABLED_TEXT,
                state="disabled" if dn_disabled else "normal",
                command=lambda i=idx: self._move_item(i, 1),
            ).grid(row=0, column=2, padx=(0, 3), pady=2)

            if not divider_entry:
                ctk.CTkButton(
                    row,
                    text="Edit",
                    width=38,
                    height=BTN_HEIGHT - 2,
                    border_spacing=0,
                    corner_radius=4,
                    font=ctk.CTkFont(family="Segoe UI", size=11),
                    fg_color=COLOR_FAST_BTN_BG,
                    hover_color=COLOR_MAIN_BTN_HOVER,
                    text_color=COLOR_MAIN_BTN_TEXT,
                    command=lambda i=idx: self._edit_item_draft(i),
                ).grid(row=0, column=3, padx=(0, 3), pady=2)

            ctk.CTkButton(
                row,
                text="✕",
                width=26,
                height=BTN_HEIGHT - 2,
                border_spacing=0,
                corner_radius=4,
                font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                fg_color=COLOR_SVC_STOP_BG,
                hover_color=COLOR_SVC_STOP_HOVER,
                text_color=COLOR_SVC_STOP_TEXT,
                command=lambda i=idx: self._remove_item_draft(i),
            ).grid(row=0, column=4, padx=(0, 3), pady=2)

    def _add_divider_draft(self) -> None:
        self._items_draft.append({"type": "divider"})
        self._render_order_list()

    def _open_add_item(self) -> None:
        base = self.base_var.get().strip() or DEFAULT_BASE_FOLDER

        def on_add(name: str, subfolder: str) -> None:
            self._items_draft.append({"name": name, "subfolder": subfolder})
            self._render_order_list()

        dlg = AddEditItemDialog(self, base, on_submit=on_add)
        dlg.bind("<Destroy>", lambda _e: self.winfo_exists() and self.after(10, self._restore_grab))

    def _edit_item_draft(self, index: int) -> None:
        if not (0 <= index < len(self._items_draft)):
            return
        item = self._items_draft[index]
        base = self.base_var.get().strip() or DEFAULT_BASE_FOLDER

        def on_edit(name: str, subfolder: str) -> None:
            self._items_draft[index] = {"name": name, "subfolder": subfolder}
            self._render_order_list()

        dlg = AddEditItemDialog(
            self,
            base,
            on_submit=on_edit,
            initial_name=item["name"],
            initial_subfolder=item["subfolder"],
            title="Edit OBS Menu Item",
        )
        dlg.bind("<Destroy>", lambda _e: self.winfo_exists() and self.after(10, self._restore_grab))

    def _remove_item_draft(self, index: int) -> None:
        if 0 <= index < len(self._items_draft):
            self._items_draft.pop(index)
            self._render_order_list()

    def _restore_grab(self) -> None:
        if self.winfo_exists():
            try:
                self.grab_set()
            except tk.TclError:
                pass

    def _move_item(self, index: int, delta: int) -> None:
        target = index + delta
        if 0 <= index < len(self._items_draft) and 0 <= target < len(self._items_draft):
            self._items_draft[index], self._items_draft[target] = (
                self._items_draft[target],
                self._items_draft[index],
            )
            self._render_order_list()

    def _grant_service_permissions(self) -> None:
        self.grant_btn.configure(state="disabled", text="Granting permissions...")

        def worker() -> None:
            ok, msg = WindowsServiceController.grant_current_user_permissions(DEFAULT_SERVICES)

            def finish() -> None:
                if not self.winfo_exists():
                    return
                self.grant_btn.configure(
                    state="normal",
                    text="Grant Start/Stop Service Permissions (Admin)",
                )
                if ok:
                    messagebox.showinfo("Service Permissions", msg, parent=self)
                else:
                    messagebox.showwarning("Service Permissions", msg, parent=self)

            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _browse_base(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Select Base Folder",
            initialdir=self.base_var.get() or DEFAULT_BASE_FOLDER,
        )
        if chosen:
            old_default = str(Path(self.base_var.get()) / "backups")
            self.base_var.set(str(Path(chosen)))
            if self.backup_var.get().strip().lower() == old_default.lower():
                self.backup_var.set(str(Path(chosen) / "backups"))

    def _browse_backup(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Select Backup Folder",
            initialdir=self.backup_var.get() or self.base_var.get(),
        )
        if chosen:
            self.backup_var.set(str(Path(chosen)))

    def _reset_backup_default(self) -> None:
        base = self.base_var.get().strip() or DEFAULT_BASE_FOLDER
        self.backup_var.set(str(Path(base) / "backups"))

    def _save(self) -> None:
        title = self.title_entry.get().strip() or APP_TITLE
        base = self.base_var.get().strip()
        backup = self.backup_var.get().strip()
        if not base:
            messagebox.showerror("Invalid Setting", "Base Folder cannot be empty.", parent=self)
            return
        if not backup:
            backup = str(Path(base) / "backups")

        self.config_mgr.title = title
        self.config_mgr.base_folder = base
        self.config_mgr.backup_folder = backup
        self.config_mgr.backup_exclude_caches = bool(self.exclude_caches_var.get())
        self.config_mgr.items = [dict(it) for it in self._items_draft]
        try:
            self.config_mgr.save()
        except OSError as exc:
            messagebox.showerror("Save Error", f"Failed to save config:\n{exc}", parent=self)
            return

        try:
            StartupManager.set_enabled(
                self.config_mgr.path, bool(self.run_at_login_var.get())
            )
        except OSError as exc:
            messagebox.showwarning(
                "Startup Setting Error",
                f"Saved config, but failed to update Windows login startup registry:\n{exc}",
                parent=self,
            )

        self.on_saved()
        self.destroy()


class AddEditItemDialog(ctk.CTkToplevel):
    """Dialog to add a new OBS menu item or edit an existing one."""

    def __init__(
        self,
        master: tk.Misc,
        base_folder: str,
        on_submit: Callable[[str, str], None],
        initial_name: str = "",
        initial_subfolder: str = "",
        title: str = "Add OBS Menu Item",
    ):
        super().__init__(master)
        self.base_folder = Path(base_folder)
        self.on_submit = on_submit

        self.title(title)
        self.geometry("520x215")
        self.resizable(False, False)
        apply_window_icon(self)
        self.transient(master)
        self.grab_set()

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
        frame.columnconfigure(1, weight=1)

        ctk.CTkLabel(frame, text="Display Name:").grid(row=0, column=0, sticky="w", pady=6)
        self.name_var = tk.StringVar(value=initial_name)
        name_entry = ctk.CTkEntry(frame, textvariable=self.name_var, placeholder_text="e.g., WMM Special 32")
        name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=6)

        ctk.CTkLabel(frame, text="OBS Subfolder:").grid(row=1, column=0, sticky="w", pady=6)
        self.subfolder_var = tk.StringVar(value=initial_subfolder)
        detected = self._detect_obs_subfolders()
        self.sub_combo = ctk.CTkComboBox(
            frame,
            variable=self.subfolder_var,
            values=detected or [""],
            command=self._on_subfolder_selected,
        )
        self.sub_combo.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=6)

        ctk.CTkButton(
            frame,
            text="Browse...",
            width=80,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COLOR_MAIN_BTN_BG,
            hover_color=COLOR_MAIN_BTN_HOVER,
            text_color=COLOR_MAIN_BTN_TEXT,
            command=self._browse_subfolder,
        ).grid(row=1, column=2, pady=6)

        ctk.CTkLabel(
            frame,
            text=f"Relative to: {self.base_folder}",
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            anchor="w",
        ).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0))

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.grid(row=3, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ctk.CTkButton(
            btn_row,
            text="Cancel",
            width=84,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color="transparent",
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_MAIN_BTN_TEXT,
            hover_color=COLOR_MAIN_BTN_HOVER,
            command=self.destroy,
        ).pack(side=tk.RIGHT, padx=(8, 0))
        ctk.CTkButton(
            btn_row,
            text="Save",
            width=84,
            height=BTN_HEIGHT,
            border_spacing=1,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            command=self._submit,
        ).pack(side=tk.RIGHT)

        self.after(50, name_entry.focus_set)

    def _detect_obs_subfolders(self) -> list[str]:
        results: list[str] = []
        if self.base_folder.is_dir():
            try:
                for child in sorted(self.base_folder.iterdir(), key=lambda p: p.name.lower()):
                    if child.is_dir() and (child / OBS_RELATIVE_EXE).is_file():
                        results.append(child.name)
            except OSError:
                pass
        return results

    def _on_subfolder_selected(self, _event: Any = None) -> None:
        if not self.name_var.get().strip():
            self.name_var.set(self.subfolder_var.get().strip())

    def _browse_subfolder(self) -> None:
        chosen = filedialog.askdirectory(
            parent=self,
            title="Select OBS Subfolder",
            initialdir=str(self.base_folder) if self.base_folder.is_dir() else DEFAULT_BASE_FOLDER,
        )
        if not chosen:
            return
        chosen_path = Path(chosen)
        try:
            rel = str(chosen_path.relative_to(self.base_folder))
        except ValueError:
            rel = str(chosen_path)
        self.subfolder_var.set(rel)
        if not self.name_var.get().strip():
            self.name_var.set(chosen_path.name)

    def _submit(self) -> None:
        name = self.name_var.get().strip()
        subfolder = self.subfolder_var.get().strip()
        if not name:
            messagebox.showerror("Missing Name", "Please enter a display name.", parent=self)
            return
        if not subfolder:
            messagebox.showerror("Missing Subfolder", "Please choose an OBS subfolder.", parent=self)
            return

        obs_dir = Path(subfolder) if Path(subfolder).is_absolute() else (self.base_folder / subfolder)
        exe_path = obs_dir / OBS_RELATIVE_EXE
        if not exe_path.is_file():
            proceed = messagebox.askyesno(
                "obs64.exe Not Found",
                f"Could not find:\n{exe_path}\n\nAdd this item anyway?",
                parent=self,
            )
            if not proceed:
                return

        self.on_submit(name, subfolder)
        self.destroy()


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------

class WmmMenuApp:
    """Main CustomTkinter GUI application for launching OBS portable instances."""

    def __init__(self, root: ctk.CTk, config_path: Path):
        self.root = root
        self.config_mgr = ConfigManager(config_path)

        # Concurrency & service tracking
        self._state_lock = threading.Lock()
        self._running_items: set[str] = set()
        self._active_obs_count: int = 0
        self._services_stopped_by_app: set[str] = set()
        self._service_busy: set[str] = set()
        self._service_statuses: dict[str, str] = {svc: "n/a" for svc in DEFAULT_SERVICES}

        # UI references & icons
        self._item_widgets: dict[str, tuple[ctk.CTkButton, ctk.CTkButton]] = {}
        self._service_buttons: dict[str, ctk.CTkButton] = {}
        self._bolt_icon, self._bolt_icon_disabled = build_lightning_icons()
        self._gear_icon = build_gear_icon()
        self._initial_placement_done = False

        self._setup_window()
        self._build_ui()
        self._render_menu_items()
        self._schedule_service_poll(immediate=True)

    def _setup_window(self) -> None:
        self.root.title(self.config_mgr.title)
        apply_window_icon(self.root)
        self.root.minsize(390, 200)
        self.root.geometry("440x260")

    def _build_ui(self) -> None:
        outer = ctk.CTkFrame(self.root, fg_color="transparent")
        outer.pack(fill=tk.BOTH, expand=True, padx=14, pady=12)
        self._outer_frame = outer

        # Header: Title + Subtitle on left, Gear Config icon button on right
        header = ctk.CTkFrame(outer, fg_color="transparent")
        header.pack(fill=tk.X, pady=(0, 8))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew")
        self.title_var = tk.StringVar(value=self.config_mgr.title)
        ctk.CTkLabel(
            title_row,
            textvariable=self.title_var,
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
            anchor="w",
        ).pack(fill=tk.X)
        self.subtitle_var = tk.StringVar()
        self._update_subtitle()
        ctk.CTkLabel(
            title_row,
            textvariable=self.subtitle_var,
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            anchor="w",
        ).pack(fill=tk.X, pady=(1, 0))

        self.config_btn = ctk.CTkButton(
            header,
            text="",
            image=self._gear_icon,
            width=28,
            height=28,
            border_spacing=1,
            corner_radius=6,
            fg_color=COLOR_MAIN_BTN_BG,
            hover_color=COLOR_MAIN_BTN_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._open_config_dialog,
        )
        self.config_btn.grid(row=0, column=1, sticky="ne", padx=(8, 0), pady=(2, 0))
        ToolTip(self.config_btn, "Configuration")

        ctk.CTkFrame(outer, height=1, fg_color=COLOR_DIVIDER).pack(fill=tk.X, pady=(0, 8))

        # Menu List (vertically auto-sized to fit all buttons)
        self.items_frame = ctk.CTkFrame(outer, fg_color="transparent")
        self.items_frame.pack(fill=tk.X, pady=(0, 10))

        # Service Status Bar
        svc_card = ctk.CTkFrame(
            outer,
            fg_color=COLOR_CARD_BG,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            corner_radius=8,
        )
        svc_card.pack(fill=tk.X, side=tk.BOTTOM)

        ctk.CTkLabel(
            svc_card,
            text="Services (click to toggle)",
            text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            anchor="w",
        ).pack(fill=tk.X, padx=8, pady=(5, 3))

        svc_row = ctk.CTkFrame(svc_card, fg_color="transparent")
        svc_row.pack(fill=tk.X, padx=8, pady=(0, 8))
        for idx, svc_name in enumerate(DEFAULT_SERVICES):
            svc_row.columnconfigure(idx, weight=1)
            btn = ctk.CTkButton(
                svc_row,
                text=f"{svc_name}: ...",
                height=BTN_HEIGHT,
                border_spacing=1,
                corner_radius=6,
                border_width=1,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                fg_color=COLOR_SVC_NA_BG,
                border_color=COLOR_SVC_NA_BORDER,
                text_color=COLOR_SVC_NA_TEXT,
                command=lambda s=svc_name: self._toggle_service(s),
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=(0 if idx == 0 else 5, 0))
            self._service_buttons[svc_name] = btn

    def _update_subtitle(self) -> None:
        self.subtitle_var.set(
            f"Config: {self.config_mgr.path.name}  |  Base: {self.config_mgr.base_folder}"
        )

    @staticmethod
    def _item_key(item: dict[str, str]) -> str:
        return f"{item['name']}||{item['subfolder'].lower()}"

    def _get_item_version(self, item: dict[str, str]) -> str | None:
        base_folder = Path(self.config_mgr.base_folder)
        sub = Path(item["subfolder"])
        obs_folder = sub if sub.is_absolute() else (base_folder / sub)
        return get_exe_product_version(obs_folder / OBS_RELATIVE_EXE)

    def _format_item_label(self, item: dict[str, str], running: bool = False) -> str:
        ver = self._get_item_version(item)
        ver_part = f"  [{ver}]" if ver else ""
        run_part = " — Running" if running else ""
        return f"  {item['name']}{ver_part}{run_part}"

    def _auto_resize_window(self) -> None:
        """Vertically resize the main window to snugly fit all menu buttons."""
        if not self.root.winfo_exists():
            return
        self.root.update_idletasks()
        scaling = getattr(self.root, "_get_window_scaling", lambda: 1.0)() or 1.0
        req_px = self._outer_frame.winfo_reqheight() + round(24 * scaling)
        logical_h = max(200, math.ceil(req_px / scaling))

        scr_logical_h = max(400, int((self.root.winfo_screenheight() / scaling) * 0.9))
        target_h = min(logical_h, scr_logical_h)

        cur_geom = self.root.geometry()  # CustomTkinter returns unscaled 'WxH+X+Y'
        cur_w = 440
        if "x" in cur_geom:
            try:
                parsed_w = int(cur_geom.split("x", 1)[0])
                if parsed_w >= 390:
                    cur_w = parsed_w
            except ValueError:
                pass

        self.root.minsize(390, target_h)
        if not self._initial_placement_done:
            self._initial_placement_done = True
            wa_left, wa_top, wa_right, wa_bottom = get_primary_work_area(self.root)
            phys_w = round(cur_w * scaling)
            phys_h = round(target_h * scaling)
            pos_x = max(wa_left, wa_left + ((wa_right - wa_left) - phys_w) // 2)
            pos_y = max(wa_top, wa_top + ((wa_bottom - wa_top) - phys_h) // 2)
            self.root.geometry(f"{cur_w}x{target_h}+{pos_x}+{pos_y}")
        else:
            self.root.geometry(f"{cur_w}x{target_h}")

    def _render_menu_items(self) -> None:
        for child in self.items_frame.winfo_children():
            child.destroy()
        self._item_widgets.clear()

        if not self.config_mgr.items:
            ctk.CTkLabel(
                self.items_frame,
                height=52,
                text="No OBS menu items configured yet.\nClick the gear icon above to add one.",
                justify="center",
                text_color=COLOR_TEXT_MUTED,
                font=ctk.CTkFont(family="Segoe UI", size=12),
            ).pack(fill=tk.X, pady=12)
            self._auto_resize_window()
            return

        with self._state_lock:
            running_snapshot = set(self._running_items)

        for idx, item in enumerate(self.config_mgr.items):
            if is_divider_item(item):
                div_wrap = ctk.CTkFrame(self.items_frame, height=14, fg_color="transparent")
                div_wrap.pack(fill=tk.X, pady=3, padx=6)
                div_line = ctk.CTkFrame(
                    div_wrap,
                    height=2,
                    corner_radius=1,
                    fg_color=COLOR_DIVIDER,
                )
                div_line.pack(fill=tk.X, pady=5)
                for w in (div_wrap, div_line):
                    w.bind(
                        "<Button-3>",
                        lambda e, i=idx: self._show_item_context_menu(e, i),
                    )
                continue

            key = self._item_key(item)
            is_running = key in running_snapshot

            # Joined container with 1px divider gap over COLOR_MENU_ROW_BORDER
            row = ctk.CTkFrame(
                self.items_frame,
                height=MENU_BTN_HEIGHT + 2,
                corner_radius=7,
                fg_color=COLOR_CARD_BORDER if is_running else COLOR_MENU_ROW_BORDER,
            )
            row.pack(fill=tk.X, pady=3, padx=2)
            row.columnconfigure(0, weight=1)  # All main buttons get identical width
            row.columnconfigure(1, weight=0)  # Attached Fast lightning button

            main_btn = ctk.CTkButton(
                row,
                text=self._format_item_label(item, running=is_running),
                height=MENU_BTN_HEIGHT,
                border_spacing=2,
                corner_radius=6,
                anchor="w",
                font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
                fg_color=COLOR_BTN_DISABLED_BG if is_running else COLOR_MENU_BTN_BG,
                hover_color=COLOR_MENU_BTN_HOVER,
                text_color=COLOR_MENU_BTN_TEXT,
                text_color_disabled=COLOR_BTN_DISABLED_TEXT,
                state="disabled" if is_running else "normal",
                command=lambda it=item: self._launch_obs(it, fast=False),
            )
            main_btn.grid(row=0, column=0, sticky="ew", padx=(1, 1), pady=1)

            fast_btn = ctk.CTkButton(
                row,
                text="",
                image=self._bolt_icon_disabled if is_running else self._bolt_icon,
                width=44,
                height=MENU_BTN_HEIGHT,
                border_spacing=2,
                corner_radius=6,
                fg_color=COLOR_BTN_DISABLED_BG if is_running else COLOR_MENU_FAST_BG,
                hover_color=COLOR_MENU_FAST_HOVER,
                state="disabled" if is_running else "normal",
                command=lambda it=item: self._launch_obs(it, fast=True),
            )
            fast_btn.grid(row=0, column=1, sticky="ew", padx=(0, 1), pady=1)
            ToolTip(fast_btn, "Start fast without a backup")

            # Right-click context menu to edit or remove item
            for widget in (main_btn, fast_btn):
                widget.bind(
                    "<Button-3>",
                    lambda e, i=idx: self._show_item_context_menu(e, i),
                )

            self._item_widgets[key] = (main_btn, fast_btn)

        self._auto_resize_window()

    def _show_item_context_menu(self, event: tk.Event, index: int) -> None:
        item = self.config_mgr.items[index]
        divider_entry = is_divider_item(item)
        is_running = False
        if not divider_entry:
            key = self._item_key(item)
            with self._state_lock:
                is_running = key in self._running_items

        is_dark = ctk.get_appearance_mode().lower() == "dark"
        menu = tk.Menu(
            self.root,
            tearoff=0,
            bg="#262c36" if is_dark else "#ffffff",
            fg="#f0f6fc" if is_dark else "#1f2328",
            activebackground="#1f6feb" if is_dark else "#dce6f4",
            activeforeground="#ffffff" if is_dark else "#1f2328",
        )
        if not divider_entry:
            menu.add_command(
                label="Edit...",
                state="disabled" if is_running else "normal",
                command=lambda: self._open_edit_dialog(index),
            )
        menu.add_command(
            label="Remove Divider" if divider_entry else "Remove",
            state="disabled" if is_running else "normal",
            command=lambda: self._remove_item(index),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def _open_add_dialog(self) -> None:
        def on_add(name: str, subfolder: str) -> None:
            self.config_mgr.items.append({"name": name, "subfolder": subfolder})
            self.config_mgr.save()
            self._render_menu_items()

        AddEditItemDialog(self.root, self.config_mgr.base_folder, on_submit=on_add)

    def _open_edit_dialog(self, index: int) -> None:
        item = self.config_mgr.items[index]

        def on_edit(name: str, subfolder: str) -> None:
            self.config_mgr.items[index] = {"name": name, "subfolder": subfolder}
            self.config_mgr.save()
            self._render_menu_items()

        AddEditItemDialog(
            self.root,
            self.config_mgr.base_folder,
            on_submit=on_edit,
            initial_name=item["name"],
            initial_subfolder=item["subfolder"],
            title="Edit OBS Menu Item",
        )

    def _remove_item(self, index: int) -> None:
        item = self.config_mgr.items[index]
        if is_divider_item(item):
            self.config_mgr.items.pop(index)
            self.config_mgr.save()
            self._render_menu_items()
            return
        if messagebox.askyesno(
            "Remove Item",
            f"Remove '{item['name']}' from the menu?",
            parent=self.root,
        ):
            self.config_mgr.items.pop(index)
            self.config_mgr.save()
            self._render_menu_items()

    def _open_config_dialog(self) -> None:
        def on_saved() -> None:
            self.root.title(self.config_mgr.title)
            self.title_var.set(self.config_mgr.title)
            self._update_subtitle()
            self._render_menu_items()

        ConfigDialog(self.root, self.config_mgr, on_saved=on_saved)

    # -----------------------------------------------------------------------
    # Service Status Polling & Toggling
    # -----------------------------------------------------------------------

    def _schedule_service_poll(self, immediate: bool = False) -> None:
        delay = 50 if immediate else SERVICE_POLL_INTERVAL_MS
        self.root.after(delay, self._poll_services_async)

    def _poll_services_async(self) -> None:
        def worker() -> None:
            results = {
                svc: WindowsServiceController.get_status(svc) for svc in DEFAULT_SERVICES
            }
            labels = {
                svc: WindowsServiceController.get_display_name(svc) for svc in DEFAULT_SERVICES
            }
            if self.root.winfo_exists():
                self.root.after(0, lambda: self._apply_service_statuses(results, labels=labels))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_service_statuses(
        self,
        results: dict[str, str],
        reschedule: bool = True,
        labels: dict[str, str] | None = None,
    ) -> None:
        self._service_statuses.update(results)
        for svc_name, status in results.items():
            btn = self._service_buttons.get(svc_name)
            if not btn:
                continue
            if svc_name in self._service_busy:
                continue
            disp = (labels or {}).get(svc_name) or WindowsServiceController.get_display_name(svc_name)
            if status == "running":
                btn.configure(
                    text=f"● {disp}: Running",
                    fg_color=COLOR_SVC_RUN_BG,
                    hover_color=COLOR_SVC_RUN_HOVER,
                    border_color=COLOR_SVC_RUN_BORDER,
                    text_color=COLOR_SVC_RUN_TEXT,
                    state="normal",
                )
            elif status == "stopped":
                btn.configure(
                    text=f"● {disp}: Stopped",
                    fg_color=COLOR_SVC_STOP_BG,
                    hover_color=COLOR_SVC_STOP_HOVER,
                    border_color=COLOR_SVC_STOP_BORDER,
                    text_color=COLOR_SVC_STOP_TEXT,
                    state="normal",
                )
            elif status == "pending":
                btn.configure(
                    text=f"◌ {disp}: Pending",
                    fg_color=COLOR_SVC_PEND_BG,
                    border_color=COLOR_SVC_PEND_BORDER,
                    text_color_disabled=COLOR_SVC_PEND_TEXT,
                    state="disabled",
                )
            else:
                btn.configure(
                    text=f"○ {disp}: N/A",
                    fg_color=COLOR_SVC_NA_BG,
                    border_color=COLOR_SVC_NA_BORDER,
                    text_color_disabled=COLOR_SVC_NA_TEXT,
                    state="disabled",
                )
        if reschedule:
            self._schedule_service_poll(immediate=False)

    def _toggle_service(self, service_name: str) -> None:
        status = self._service_statuses.get(service_name, "n/a")
        if status not in ("running", "stopped"):
            return
        if service_name in self._service_busy:
            return

        self._service_busy.add(service_name)
        btn = self._service_buttons[service_name]
        disp = WindowsServiceController.get_display_name(service_name)
        action_label = "Stopping" if status == "running" else "Starting"
        btn.configure(
            text=f"◌ {disp}: {action_label}...",
            fg_color=COLOR_SVC_PEND_BG,
            border_color=COLOR_SVC_PEND_BORDER,
            text_color_disabled=COLOR_SVC_PEND_TEXT,
            state="disabled",
        )

        def worker() -> None:
            if status == "running":
                ok, msg = WindowsServiceController.stop_service(service_name)
            else:
                ok, msg = WindowsServiceController.start_service(service_name)
            new_status = WindowsServiceController.get_status(service_name)
            new_disp = WindowsServiceController.get_display_name(service_name)

            def finish() -> None:
                self._service_busy.discard(service_name)
                self._apply_service_statuses(
                    {service_name: new_status},
                    reschedule=False,
                    labels={service_name: new_disp},
                )
                if not ok:
                    messagebox.showwarning(
                        f"Service '{new_disp}'",
                        msg,
                        parent=self.root,
                    )

            self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    # -----------------------------------------------------------------------
    # Threaded OBS Launch Workflow
    # -----------------------------------------------------------------------

    def _set_item_running_ui(self, item: dict[str, str], running: bool) -> None:
        key = self._item_key(item)
        pair = self._item_widgets.get(key)
        if not pair:
            return
        main_btn, fast_btn = pair
        row = main_btn.master
        if running:
            row.configure(fg_color=COLOR_CARD_BORDER)
            main_btn.configure(
                text=self._format_item_label(item, running=True),
                state="disabled",
                fg_color=COLOR_BTN_DISABLED_BG,
            )
            fast_btn.configure(
                image=self._bolt_icon_disabled,
                state="disabled",
                fg_color=COLOR_BTN_DISABLED_BG,
            )
        else:
            row.configure(fg_color=COLOR_MENU_ROW_BORDER)
            main_btn.configure(
                text=self._format_item_label(item, running=False),
                state="normal",
                fg_color=COLOR_MENU_BTN_BG,
            )
            fast_btn.configure(
                image=self._bolt_icon,
                state="normal",
                fg_color=COLOR_MENU_FAST_BG,
            )

    def _launch_obs(self, item: dict[str, str], fast: bool) -> None:
        key = self._item_key(item)
        with self._state_lock:
            if key in self._running_items:
                return
            self._running_items.add(key)

        self._set_item_running_ui(item, running=True)
        popup = LaunchProgressWindow(self.root, item["name"], item["subfolder"], fast=fast)

        base_folder = Path(self.config_mgr.base_folder)
        backup_folder = Path(self.config_mgr.backup_folder)
        exclude_caches = self.config_mgr.backup_exclude_caches
        subfolder_path = Path(item["subfolder"])
        obs_folder = subfolder_path if subfolder_path.is_absolute() else (base_folder / subfolder_path)

        thread = threading.Thread(
            target=self._obs_worker,
            args=(item, key, obs_folder, backup_folder, exclude_caches, fast, popup),
            daemon=True,
        )
        thread.start()

    def _ui(self, fn: Callable[[], None]) -> None:
        if self.root.winfo_exists():
            self.root.after(0, fn)

    def _ask_yes_no_sync(self, title: str, message: str) -> bool:
        """Ask a Yes/No question on the Tk main thread and wait for the answer."""
        done_evt = threading.Event()
        answer = [False]

        def ask() -> None:
            try:
                if self.root.winfo_exists():
                    answer[0] = messagebox.askyesno(title, message, parent=self.root)
            finally:
                done_evt.set()

        self._ui(ask)
        done_evt.wait()
        return answer[0]

    def _obs_worker(
        self,
        item: dict[str, str],
        key: str,
        obs_folder: Path,
        backup_folder: Path,
        exclude_caches: bool,
        fast: bool,
        popup: LaunchProgressWindow,
    ) -> None:
        had_error = False
        obs_error_msg: str | None = None
        counted_session = False
        popup_hidden = False

        def log(msg: str) -> None:
            self._ui(lambda: popup.winfo_exists() and popup.append_log(msg))

        def step(msg: str, indeterminate: bool = True) -> None:
            self._ui(lambda: popup.winfo_exists() and popup.set_step(msg, indeterminate))

        def detail(msg: str) -> None:
            self._ui(lambda: popup.winfo_exists() and popup.set_detail(msg))

        def restore_popup() -> None:
            nonlocal popup_hidden
            if popup_hidden:
                popup_hidden = False
                self._ui(
                    lambda: popup.winfo_exists()
                    and (
                        popup.geometry(f"+{popup._pos_x}+{popup._pos_y}"),
                        popup.deiconify(),
                        popup.lift(),
                    )
                )

        try:
            obs_exe = obs_folder / OBS_RELATIVE_EXE
            obs_bin_dir = obs_exe.parent
            if not obs_folder.is_dir():
                raise FileNotFoundError(f"OBS folder does not exist:\n{obs_folder}")
            if not obs_exe.is_file():
                raise FileNotFoundError(f"OBS executable not found:\n{obs_exe}")

            # 1. Track active session & stop sync services if running
            with self._state_lock:
                self._active_obs_count += 1
                counted_session = True

            step("Checking sync services...")
            for svc in DEFAULT_SERVICES:
                disp = WindowsServiceController.get_display_name(svc)
                status = WindowsServiceController.get_status(svc)
                if status == "n/a":
                    log(f"'{disp}': not installed (skipping).")
                elif status == "stopped":
                    log(f"'{disp}': already stopped.")
                else:
                    log(f"Stopping '{disp}'...")
                    ok, msg = WindowsServiceController.stop_service(svc)
                    log(msg)
                    if ok:
                        with self._state_lock:
                            self._services_stopped_by_app.add(svc)
                    else:
                        had_error = True
            self._poll_services_async()

            # 2. Backup config (unless Fast mode)
            if fast:
                log("Fast mode enabled — skipping config backup.")
            else:
                step("Backing up OBS configuration...", indeterminate=False)
                mode_note = " (excluding caches/logs)" if exclude_caches else " (full config)"
                log(f"Backing up '{obs_folder / 'config'}' to '{backup_folder}'{mode_note}...")

                last_ui_update = 0.0

                def on_progress(done: int, total: int, current_file: str) -> None:
                    nonlocal last_ui_update
                    now = time.monotonic()
                    if done == 0 or done >= total or (now - last_ui_update) >= 0.05:
                        last_ui_update = now
                        self._ui(
                            lambda d=done, t=total, f=current_file: popup.winfo_exists()
                            and popup.set_progress(d, t, f)
                        )

                try:
                    archive_path = backup_obs_config(
                        obs_folder,
                        backup_folder,
                        on_progress,
                        exclude_caches=exclude_caches,
                    )
                    log(f"Backup complete: {archive_path.name}")
                except Exception as exc:
                    had_error = True
                    log(f"ERROR during backup: {exc}")
                    proceed = self._ask_yes_no_sync(
                        "Backup Error",
                        f"Failed to back up config for '{item['name']}':\n{exc}\n\n"
                        "Do you still want to launch OBS without a backup?",
                    )
                    if not proceed:
                        log("Launch aborted by user after backup failure.")
                        return

            # 3. Start OBS in portable mode, hide popup while running, wait for exit
            step("Running OBS (portable mode)...", indeterminate=True)
            cmd = [str(obs_exe), *OBS_DEFAULT_ARGS]
            log(f"Starting: {' '.join(cmd)}")
            log(f"Working directory: {obs_bin_dir}")

            proc = subprocess.Popen(
                cmd,
                cwd=str(obs_bin_dir),
                stderr=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                text=True,
                errors="replace",
            )
            detail(f"PID {proc.pid} — waiting for OBS to exit...")
            popup_hidden = True
            self._ui(lambda: popup.winfo_exists() and popup.withdraw())

            _stdout, stderr_output = proc.communicate()
            returncode = proc.returncode

            if returncode != 0:
                had_error = True
                restore_popup()
                snippet = (stderr_output or "").strip()
                if len(snippet) > 800:
                    snippet = snippet[-800:]
                obs_error_msg = (
                    f"OBS ({item['name']}) exited with error code {returncode}."
                    + (f"\n\nDetails:\n{snippet}" if snippet else "")
                )
                log(f"ERROR: OBS exited with code {returncode}.")
            else:
                log("OBS exited cleanly (code 0).")

        except Exception as exc:
            had_error = True
            restore_popup()
            obs_error_msg = f"Failed to run OBS ({item['name']}):\n{exc}"
            log(f"ERROR: {exc}")

        finally:
            # 4. Restart services we stopped (only when last OBS instance exits)
            services_to_restart: list[str] = []
            with self._state_lock:
                if counted_session:
                    self._active_obs_count = max(0, self._active_obs_count - 1)
                if self._active_obs_count == 0 and self._services_stopped_by_app:
                    services_to_restart = sorted(self._services_stopped_by_app)
                    self._services_stopped_by_app.clear()

            if services_to_restart:
                restore_popup()
                step("Restarting sync services...", indeterminate=True)
                detail("")
                for svc in services_to_restart:
                    disp = WindowsServiceController.get_display_name(svc)
                    log(f"Restarting '{disp}'...")
                    ok, msg = WindowsServiceController.start_service(svc)
                    log(msg)
                    if not ok:
                        had_error = True
                self._poll_services_async()
            elif counted_session:
                with self._state_lock:
                    remaining = self._active_obs_count
                if remaining > 0:
                    log(f"{remaining} other OBS instance(s) still running — keeping sync services stopped.")

            # 5. Re-enable button & show any OBS error popup
            with self._state_lock:
                self._running_items.discard(key)

            def finalize_ui() -> None:
                self._set_item_running_ui(item, running=False)
                if obs_error_msg:
                    messagebox.showerror(
                        f"OBS Error - {item['name']}",
                        obs_error_msg,
                        parent=self.root,
                    )
                if popup.winfo_exists():
                    if popup_hidden and not had_error:
                        popup.destroy()
                    else:
                        popup.set_step(
                            "Finished" if not had_error else "Finished with errors",
                            indeterminate=False,
                        )
                        popup.set_detail("")
                        popup.mark_finished(auto_close=not had_error)

            self._ui(finalize_ui)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WMM OBS Portable Launcher with config backup and service management."
    )
    parser.add_argument(
        "config_pos",
        nargs="?",
        default=None,
        help=f"Optional path to YAML config file (default: {DEFAULT_CONFIG_FILENAME})",
    )
    parser.add_argument(
        "-c",
        "--config",
        dest="config_opt",
        default=None,
        help=f"Path to YAML config file (default: {DEFAULT_CONFIG_FILENAME})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    chosen = args.config_opt or args.config_pos
    if chosen:
        config_path = Path(chosen).expanduser().resolve()
    else:
        config_path = get_app_dir() / DEFAULT_CONFIG_FILENAME

    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    WmmMenuApp(root, config_path)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
