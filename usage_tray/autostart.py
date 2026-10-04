"""Start-with-Windows toggle via the per-user Run registry key (no admin rights needed)."""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # not Windows: toggle is a no-op
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "usage-tray"


def command() -> str:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.exists():
        exe = pythonw
    main = Path(__file__).resolve().parent.parent / "usage_tray.pyw"
    return f'"{exe}" "{main}"'


def is_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE)
            return True
    except OSError:
        return False


def set_enabled(on: bool) -> None:
    if winreg is None:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass
