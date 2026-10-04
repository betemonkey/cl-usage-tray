"""Tray app: polls the transcripts, refits the estimate, updates icon and tooltip."""
from __future__ import annotations

import gc
import logging
import queue
import threading
import time

from . import autostart, estimator as est
from .config import APP_DIR, load_config, load_samples, save_samples
from .parser import LogStore

log = logging.getLogger("usage-tray")
KEEP_DAYS = 2  # calls older than this are dropped from memory once their lockouts are sampled


def get_tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # tzdata missing: fall back to the machine's local time
        log.warning("timezone %s unavailable, using local time", name)
        return None


class Monitor:
    """Everything except the UI: read logs, keep lockout samples, compute a Status."""

    def __init__(self, cfg: dict, store: LogStore | None = None, samples: dict | None = None,
                 persist: bool = True):
        self.cfg = cfg
        self.store = store or LogStore()
        self.samples = load_samples() if samples is None else samples
        self.persist = persist
        self.tz = get_tz(cfg["timezone"])
        self.scanned = False
        self.complete_since = 0.0  # calls from this time on are all in memory
        self.status = est.Status()
        self.lock = threading.Lock()

    def refresh(self, now: float | None = None) -> est.Status:
        with self.lock:
            now = now or time.time()
            # The first scan reads all history (about 2 s) to find old lockouts; later polls are incremental.
            since = now - self.cfg["recent_hours"] * 3600 if self.scanned else 0
            self.store.poll(since)
            self.scanned = True
            calls = self.store.sorted_calls()
            lockouts = list(self.store.lockouts.values())
            self._update_samples(calls, lockouts)
            self.complete_since = now - KEEP_DAYS * 86400
            self.store.prune(self.complete_since)

            excluded = {float(x) for x in self.cfg["exclude_lockouts"]}
            samples = [s for k, s in self.samples.items() if k not in excluded]
            cal = est.fit(samples, self.cfg["prices"], now, self.cfg["multipliers"])
            if self.cfg["limit"]:
                cal.limit = float(self.cfg["limit"])
            self.status = est.compute_status(calls, lockouts, cal, self.cfg["prices"], now,
                                             self.tz, self.cfg["live_minutes"])
            return self.status

    def _update_samples(self, calls, lockouts) -> None:
        changed = False
        for s in est.lockout_samples(calls, lockouts):
            known = self.samples.get(s.resets_at)
            if s.resets_at - est.WINDOW < self.complete_since:
                continue  # part of this window was already pruned
            if known is None or known.rejected_at != s.rejected_at or known.tokens != s.tokens:
                self.samples[s.resets_at] = s
                changed = True
        if changed and self.persist:
            save_samples(self.samples)


class DetailsUI(threading.Thread):
    """Owns Tk for the whole app lifetime: Tk objects must be created and freed on one thread."""

    def __init__(self, monitor: Monitor):
        super().__init__(daemon=True)
        self.monitor = monitor
        self.requests: queue.Queue[str] = queue.Queue()
        self.window = None

    def open(self) -> None:
        self.requests.put("open")

    def close_all(self) -> None:
        self.requests.put("quit")
        self.join(timeout=2)

    def run(self) -> None:
        import tkinter as tk

        self.tk = tk
        root = tk.Tk()
        root.withdraw()
        self.root = root
        root.after(100, self._poll)
        root.mainloop()
        root.destroy()
        # Free every Tk object here; if the main thread frees them, Tcl aborts the process.
        self.root = self.window = root = None
        gc.collect()

    def _poll(self) -> None:
        while not self.requests.empty():
            req = self.requests.get_nowait()
            if req == "quit":
                self.root.quit()
                return
            self._show()
        self.root.after(150, self._poll)

    def _show(self) -> None:
        tk = self.tk
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            return
        win = self.window = tk.Toplevel(self.root)
        win.title("usage-tray details")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        frame = tk.Frame(win, padx=14, pady=12)
        frame.pack()
        tk.Label(frame, text="Current 5-hour window (estimate)", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        tk.Label(frame, text="The real plan % is not in the logs; this is fitted from past lockouts.",
                 font=("Segoe UI", 8), fg="#777").grid(row=99, column=0, columnspan=2, sticky="w", pady=(8, 0))
        labels = []

        def fill():
            if not win.winfo_exists():
                return
            for lbl in labels:
                lbl.destroy()
            labels.clear()
            for i, (k, v) in enumerate(est.details(self.monitor.status, time.time(), self.monitor.tz), start=1):
                a = tk.Label(frame, text=k, font=("Segoe UI", 9), fg="#555")
                b = tk.Label(frame, text=v, font=("Segoe UI", 9), justify="right")
                a.grid(row=i, column=0, sticky="w", padx=(0, 18))
                b.grid(row=i, column=1, sticky="e")
                labels.extend((a, b))
            win.after(5000, fill)

        fill()


class TrayApp:
    def __init__(self, monitor: Monitor):
        import pystray

        self.pystray = pystray
        self.monitor = monitor
        self.stop = threading.Event()
        self.details = DetailsUI(monitor)
        menu = pystray.Menu(
            pystray.MenuItem("Refresh now", lambda: self.update()),
            pystray.MenuItem("Details…", lambda: self.details.open(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self.toggle_autostart,
                             checked=lambda item: autostart.is_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )
        from .icon import ring_icon
        self.icon = pystray.Icon("usage-tray", ring_icon(0), "Claude usage: starting…", menu)

    def update(self) -> None:
        from .icon import lock_icon, ring_icon

        try:
            st = self.monitor.refresh()
        except Exception:
            log.exception("refresh failed")
            self.icon.title = "Claude usage: error reading logs (see usage-tray.log)"
            return
        now = time.time()
        cfg = self.monitor.cfg
        self.icon.icon = (lock_icon(st.reset_at - now) if st.locked
                          else ring_icon(st.pct, cfg["yellow_at"], cfg["red_at"]))
        self.icon.title = est.tooltip(st, now, self.monitor.tz)

    def loop(self) -> None:
        while not self.stop.is_set():
            self.update()
            self.stop.wait(self.monitor.cfg["poll_seconds"])

    def toggle_autostart(self, icon, item) -> None:
        autostart.set_enabled(not autostart.is_enabled())

    def quit(self, icon, item) -> None:
        self.stop.set()
        self.details.close_all()
        icon.stop()

    def run(self) -> None:
        def setup(icon):
            icon.visible = True
            self.details.start()
            threading.Thread(target=self.loop, daemon=True).start()
        self.icon.run(setup)


def single_instance() -> bool:
    """True if no other usage-tray is running (named mutex, held until exit)."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        single_instance.handle = kernel32.CreateMutexW(None, False, "Local\\usage-tray")
        return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except AttributeError:  # not Windows
        return True


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=APP_DIR / "usage-tray.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not single_instance():
        return
    TrayApp(Monitor(load_config())).run()
