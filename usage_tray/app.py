"""Tray app: polls the transcripts, refits the estimate, updates icon and hover card."""
from __future__ import annotations

import logging
import threading
import time

from . import autostart, estimator as est
from .config import (APP_DIR, load_config, load_readings, load_samples, save_readings,
                     save_samples)
from .parser import LogStore

log = logging.getLogger("usage-tray")
KEEP_DAYS = 8  # keep a full weekly window of calls in memory


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
                 readings: dict | None = None, persist: bool = True):
        self.cfg = cfg
        self.store = store or LogStore()
        self.samples = load_samples() if samples is None else samples
        self.readings = load_readings() if readings is None else readings
        self.persist = persist
        self.tz = get_tz(cfg["timezone"])
        self.scanned = False
        self.complete_since = 0.0  # calls from this time on are all in memory
        self.status = est.Status()
        self.updated = 0.0
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
            self.status = self._compute(calls, lockouts, now)
            self.updated = now
            return self.status

    def _calibration(self, now: float) -> est.Calibration:
        excluded = {float(x) for x in self.cfg["exclude_lockouts"]}
        samples = [s for k, s in self.samples.items() if k not in excluded]
        cal = est.fit(samples, self.cfg["prices"], now, self.cfg["multipliers"])
        session = self.readings.get("session")
        if self.cfg["limit"]:
            cal.limit, cal.source = float(self.cfg["limit"]), "config"
        elif session and session.get("limit"):
            cal.limit, cal.source = session["limit"], "claude.ai"
        return cal

    def _compute(self, calls, lockouts, now) -> est.Status:
        week_limits = {k: self.readings[k]["limit"] for k in ("week", "fable")
                       if self.readings.get(k, {}).get("limit")}
        return est.compute_status(calls, lockouts, self._calibration(now), self.cfg["prices"], now,
                                  self.tz, self.cfg["live_minutes"], week_limits,
                                  tuple(self.cfg["week_reset"]))

    def match(self, session: float | None, week: float | None, fable: float | None,
              now: float | None = None) -> list[str]:
        """Store claude.ai readings (percentages) as limits. Returns the names that were set."""
        with self.lock:
            now = now or time.time()
            st = self._compute(self.store.sorted_calls(), list(self.store.lockouts.values()), now)
            # Readings scale our weighted cost, which uses the current multipliers.
            costs = {"session": st.window_cost, "week": st.week.cost, "fable": st.fable.cost}
            done = []
            for name, pct in (("session", session), ("week", week), ("fable", fable)):
                limit = est.limit_from_reading(costs[name], pct)
                if limit:
                    self.readings[name] = {"limit": limit, "pct": pct, "at": now}
                    done.append(name)
            if done and self.persist:
                save_readings(self.readings)
            self.status = self._compute(self.store.sorted_calls(), list(self.store.lockouts.values()), now)
            return done

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


class TrayApp:
    def __init__(self, monitor: Monitor):
        import pystray

        from .icon import ring_icon
        from .ui import UI

        self.monitor = monitor
        self.stop = threading.Event()
        self.hover = monitor.cfg["hover_card"]
        menu = pystray.Menu(
            pystray.MenuItem("Usage card", lambda: self.ui.toggle_card(), default=True, visible=False),
            pystray.MenuItem("Refresh now", lambda: self.update()),
            pystray.MenuItem("Details…", lambda: self.ui.open_details()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self.toggle_autostart,
                             checked=lambda item: autostart.is_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )
        self.icon = pystray.Icon("usage-tray", ring_icon(0), "Claude usage: starting…", menu)
        self.ui = UI(monitor, self.icon_rect if self.hover else None, refresh=self.update)

    def icon_rect(self):
        from .ui import notify_icon_rect
        return notify_icon_rect(getattr(self.icon, "_hwnd", None), id(self.icon))

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
        # With the hover card on, an empty title keeps the plain tooltip from showing on top of it.
        self.icon.title = "" if self.hover else est.tooltip(st, now, self.monitor.tz)
        self.ui.request("refresh")

    def loop(self) -> None:
        while not self.stop.is_set():
            self.update()
            self.stop.wait(self.monitor.cfg["poll_seconds"])

    def toggle_autostart(self, icon, item) -> None:
        autostart.set_enabled(not autostart.is_enabled())

    def quit(self, icon, item) -> None:
        self.stop.set()
        self.ui.close_all()
        icon.stop()

    def run(self) -> None:
        def setup(icon):
            icon.visible = True
            self.ui.start()
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


def dpi_aware() -> None:
    """Sharp text on scaled displays, and one pixel unit for cursor, tray and Tk positions."""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        pass


def main() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=APP_DIR / "usage-tray.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if not single_instance():
        return
    dpi_aware()
    TrayApp(Monitor(load_config())).run()
