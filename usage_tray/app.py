"""Tray app: polls the transcripts, refits the estimate, updates icon and hover card."""
from __future__ import annotations

import logging
import threading
import time

from . import autostart, estimator as est, live
from .config import (APP_DIR, load_config, load_readings, load_samples, save_readings,
                     save_samples)
from .parser import LogStore

log = logging.getLogger("usage-tray")
KEEP_DAYS = 8  # keep a full weekly window of calls in memory
AUTO_MATCH_MIN_PCT = 20     # live readings below this are too coarse (whole %) to calibrate from
AUTO_MATCH_EVERY = 30 * 60  # and recalibrating more often than this only adds noise


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
        self.exact: live.LiveUsage | None = None  # last good reading from Anthropic's usage endpoint
        self.exact_error: str | None = None
        self.next_fetch = 0.0

    def _fetch_live(self, now: float) -> None:
        """Ask Anthropic for the exact numbers every api_seconds (outside the lock: it can take seconds)."""
        if not self.cfg["live_api"] or now < self.next_fetch:
            return
        interval = self.cfg["api_seconds"]
        try:
            self.exact, self.exact_error = live.get_usage(now), None
        except live.LiveError as e:
            self.exact_error = str(e)
            if "rate limiting" in self.exact_error:
                interval *= 4
            log.info("live usage unavailable: %s", e)
        self.next_fetch = now + interval

    def _exact_fresh(self, now: float):
        stale_after = max(3 * self.cfg["api_seconds"], 600)
        return self.exact if self.exact and now - self.exact.fetched_at < stale_after else None

    def refresh(self, now: float | None = None) -> est.Status:
        now = now or time.time()
        self._fetch_live(now)
        with self.lock:
            # The first scan reads all history (about 2 s) to find old lockouts; later polls are incremental.
            since = now - self.cfg["recent_hours"] * 3600 if self.scanned else 0
            self.store.poll(since)
            self.scanned = True
            calls = self.store.sorted_calls()
            lockouts = list(self.store.lockouts.values())
            self._update_samples(calls, lockouts)
            self.complete_since = now - KEEP_DAYS * 86400
            self.store.prune(self.complete_since)
            self._auto_match(calls, lockouts, now)
            self.status = self._compute(calls, lockouts, now)
            self.updated = now
            return self.status

    def _auto_match(self, calls, lockouts, now) -> None:
        """Keep the offline estimate calibrated from live readings, so it is close when live data is gone."""
        exact = self._exact_fresh(now)
        if not exact or exact.fetched_at != now:
            return
        fable = next((w for w in exact.weekly if w.name.startswith("Fable")), None)
        week = next((w for w in exact.weekly if w.name == "This week"), None)
        wanted = {"session": exact.session, "week": week, "fable": fable}
        values = {k: l.pct for k, l in wanted.items()
                  if l and l.pct >= AUTO_MATCH_MIN_PCT and l.pct < 100
                  and now - self.readings.get(k, {}).get("at", 0) >= AUTO_MATCH_EVERY}
        if values:
            self._match(values, calls, lockouts, now)

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
        st = est.compute_status(calls, lockouts, self._calibration(now), self.cfg["prices"], now,
                                self.tz, self.cfg["live_minutes"], week_limits,
                                tuple(self.cfg["week_reset"]))
        st.exact, st.exact_error = self._exact_fresh(now), self.exact_error
        if st.exact and st.exact.session:  # exact numbers win over the estimate for icon and card
            s = st.exact.session
            st.pct, st.locked = s.pct, s.pct >= 100
            if s.resets_at:
                st.reset_at, st.window_start = s.resets_at, s.resets_at - est.WINDOW
        return st

    def _match(self, values: dict, calls, lockouts, now) -> list[str]:
        st = self._compute(calls, lockouts, now)
        # Readings scale our weighted cost, which uses the current multipliers.
        costs = {"session": st.window_cost, "week": st.week.cost, "fable": st.fable.cost}
        done = []
        for name in ("session", "week", "fable"):
            limit = est.limit_from_reading(costs[name], values.get(name))
            if limit:
                self.readings[name] = {"limit": limit, "pct": values[name], "at": now}
                done.append(name)
        if done and self.persist:
            save_readings(self.readings)
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
