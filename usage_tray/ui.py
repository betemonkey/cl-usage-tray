"""Hover card and details window (mockup A: one row per limit, like claude.ai's usage page), in Tk.

Tk objects must be created and freed on one thread, so a single UI thread owns all of them and
takes requests through a queue. It also polls the cursor to open the card when it rests on the icon.
"""
from __future__ import annotations

import ctypes
import gc
import queue
import threading
import time
from ctypes import wintypes
from datetime import datetime

from . import estimator as est

STATUS = {"good": "#0ca30c", "warn": "#fab219", "crit": "#d03b3b"}
PALETTES = {
    "dark": {"surface": "#202020", "surface2": "#2b2b2b", "stroke": "#353535", "ink": "#f2f2f2",
             "ink2": "#c5c5c5", "ink3": "#8f8f8f", "idle": "#3a3a3a",
             "track": {"good": "#1f3a1f", "warn": "#3d3218", "crit": "#432121"}},
    "light": {"surface": "#f9f9f9", "surface2": "#ffffff", "stroke": "#e3e3e3", "ink": "#1b1b1b",
              "ink2": "#5c5c5c", "ink3": "#7a7a7a", "idle": "#e3e3e3",
              "track": {"good": "#d3ecd3", "warn": "#fbe9c1", "crit": "#f3d0d0"}},
}
HOVER_DELAY, LEAVE_DELAY, PINNED_LEAVE_DELAY = 0.35, 0.4, 3.0


# ---------------------------------------------------------------- what to show (no Tk, testable)

def severity(pct: float, yellow_at: float = 60, red_at: float = 85) -> str:
    return "crit" if pct > red_at else "warn" if pct >= yellow_at else "good"


def limit_rows(st: est.Status, now: float, tz=None, yellow_at: float = 60, red_at: float = 85) -> list[dict]:
    """One dict per limit: name, pct (None = unknown), value text, sub text, severity."""
    sev = lambda p: severity(p, yellow_at, red_at)
    if st.locked:
        session = dict(pct=100, value="Locked", sev="crit",
                       sub=f"Resets {est.fmt_clock(st.reset_at, tz)}, in {est.fmt_duration(st.reset_at - now)}")
    elif st.reset_at:
        session = dict(pct=st.pct, value=f"{st.pct:.0f}% used", sev=sev(st.pct),
                       sub=f"Resets {est.fmt_clock(st.reset_at, tz)}, in {est.fmt_duration(st.reset_at - now)}")
    else:
        session = dict(pct=0, value="0% used", sev="good", sub="No session running. Your next message starts one.")
    rows = [dict(name="Current session", **session)]

    if st.exact:  # exact numbers from Anthropic
        for w in st.exact.weekly:
            reset = est.fmt_weekly_reset(w.resets_at, tz) if w.resets_at else "?"
            model = w.name[:-len(" this week")] if w.name.endswith(" this week") else ""
            sub = f"Separate {model} limit, resets {reset}" if model else f"Resets {reset}"
            rows.append(dict(name=w.name, pct=w.pct, value=f"{w.pct:.0f}% used", sev=sev(w.pct), sub=sub))
        c = st.exact.credit
        if c:
            expires = datetime.fromtimestamp(c.resets_at, tz).strftime("%d %b %H:%M") if c.resets_at else "?"
            rows.append(dict(name="Cloud session credits", pct=c.used / c.limit * 100, sev="neutral",
                             value=f"${c.left:.0f} of ${c.limit:.0f} left", sub=f"Expires {expires}"))
        return rows

    for name, label, extra in (("week", "This week", ""), ("fable", "Fable this week", "Separate Fable limit, ")):
        w = getattr(st, name)
        if w is None:
            continue
        reset = est.fmt_weekly_reset(w.reset_at, tz)
        if w.pct is None:
            rows.append(dict(name=label, pct=None, value="Not set", sev="good",
                             sub="Waiting for a live reading to calibrate"))
        else:
            sub = f"{extra}resets {reset}" if extra else f"Resets {reset}"
            rows.append(dict(name=label, pct=w.pct, value=f"{w.pct:.0f}% used", sev=sev(w.pct), sub=sub))
    return rows


def footer_text(st: est.Status, updated: float, tz, poll_seconds: int, api_seconds: int) -> str:
    if st.exact:
        return f"Live from Anthropic at {est.fmt_clock(st.exact.fetched_at, tz)}, every {api_seconds // 60} min"
    why = f" ({st.exact_error})" if st.exact_error else ""
    return f"Estimate{why}, updated {est.fmt_clock(updated, tz)}"


def place_near(anchor, size, work, gap: int = 12) -> tuple[int, int]:
    """Top-left for a window of `size` that grows up and left from the anchor's bottom-right corner
    (where the card was), kept inside the work area."""
    (al, at, ar, ab), (w, h), (left, top, right, bottom) = anchor, size, work
    x = min(max(ar - w, left + gap), right - w - gap)
    y = min(max(ab - h, top + gap), bottom - h - gap)
    return max(x, left), max(y, top)


# ---------------------------------------------------------------- Windows helpers

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.ULONG), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8)]


class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("guidItem", _GUID)]


def notify_icon_rect(hwnd, icon_id: int):
    """Screen rectangle (left, top, right, bottom) of our tray icon, or None if Windows won't say."""
    if not hwnd:
        return None
    try:
        get_rect = ctypes.windll.shell32.Shell_NotifyIconGetRect
    except AttributeError:
        return None
    # pystray passes its id under a misspelled field, so Windows registers the icon as uID 0.
    for uid in (0, icon_id & 0xFFFFFFFF):
        ident = _NOTIFYICONIDENTIFIER(ctypes.sizeof(_NOTIFYICONIDENTIFIER), hwnd, uid)
        rect = wintypes.RECT()
        if get_rect(ctypes.byref(ident), ctypes.byref(rect)) == 0:  # S_OK
            return rect.left, rect.top, rect.right, rect.bottom
    return None


def cursor_pos():
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def work_area(x: int, y: int):
    """Work area (excluding the taskbar) of the monitor containing the point."""
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    mon = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO(ctypes.sizeof(MONITORINFO))
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


def system_theme() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "dark"


def _colorref(hex_color: str) -> int:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return r | g << 8 | b << 16


def style_window(win, dark: bool, border: str | None = None) -> bool:
    """Windows 11 rounded corners, a theme-matching title bar and, for borderless windows, a 1px
    border that follows the rounded shape. Call after the window is shown. False if not applied."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)  # GA_ROOT: the real top-level window
        dwm = ctypes.windll.dwmapi
        attrs = [(20, int(dark)), (33, 2)]  # IMMERSIVE_DARK_MODE, WINDOW_CORNER_PREFERENCE = ROUND
        if border:
            attrs.append((34, _colorref(border)))  # BORDER_COLOR
        ok = True
        for attr, value in attrs:
            v = ctypes.c_uint(value)
            ok &= dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0
        return ok
    except Exception:
        return False


# ---------------------------------------------------------------- the UI thread

class UI(threading.Thread):
    def __init__(self, monitor, icon_rect=None, refresh=None):
        super().__init__(daemon=True)
        self.monitor = monitor
        self.icon_rect = icon_rect  # callable -> rect, or None when the hover card is off
        self.refresh_cb = refresh
        self.requests: queue.Queue[str] = queue.Queue()
        self.card = self.details = None
        self.pinned = False
        self.hover_since = self.away_since = None

    # requests from other threads
    def request(self, what: str) -> None:
        self.requests.put(what)

    def toggle_card(self) -> None:
        self.request("toggle_card")

    def open_details(self) -> None:
        self.request("details")

    def close_all(self) -> None:
        self.request("quit")
        self.join(timeout=2)

    # thread body
    def run(self) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        family = "Segoe UI Variable Text" if "Segoe UI Variable Text" in tkfont.families() else "Segoe UI"
        self.f = {k: (family, size, weight) for k, size, weight in (
            ("body", 10, "normal"), ("small", 9, "normal"), ("name", 10, "bold"), ("head", 11, "bold"),
            ("stat", 14, "bold"), ("tag", 8, "normal"))}
        self.s = self.root.winfo_fpixels("1i") / 96  # pixel sizes below are at 100% scaling
        self.root.after(100, self._tick)
        self.root.mainloop()
        self.root.destroy()
        # Free every Tk object here; if the main thread frees them, Tcl aborts the process.
        for name, value in list(vars(self).items()):
            if isinstance(value, (tk.Misc, tk.Image)):
                setattr(self, name, None)
        self.tk = None
        gc.collect()

    def px(self, n: float) -> int:
        return int(round(n * self.s))

    def _tick(self) -> None:
        while not self.requests.empty():
            req = self.requests.get_nowait()
            if req == "quit":
                self.root.quit()
                return
            if req == "toggle_card":  # left click: pin a hover-opened card, close a pinned one
                if self.card is not None and self.pinned:
                    self._hide_card()
                else:
                    self._show_card(pinned=True)
            elif req == "details":
                self._show_details()
            elif req == "refresh":
                if self.card is not None:
                    self._fill_card()
                if self.details is not None:
                    self._fill_details()
        if self.icon_rect is not None:
            try:
                self._track_hover()
            except Exception:
                pass
        self.root.after(120, self._tick)

    def _track_hover(self) -> None:
        x, y = cursor_pos()
        rect = self.icon_rect()
        inside = lambda r: r and r[0] <= x < r[2] and r[1] <= y < r[3]
        on_icon = inside(rect)
        now = time.monotonic()
        self.hover_since = (self.hover_since or now) if on_icon else None
        if self.card is None:
            if self.hover_since and now - self.hover_since >= HOVER_DELAY:
                self._show_card(pinned=False)
            return
        c = self.card
        card_rect = (c.winfo_rootx(), c.winfo_rooty(), c.winfo_rootx() + c.winfo_width(),
                     c.winfo_rooty() + c.winfo_height())
        if on_icon or inside(card_rect):
            self.away_since = None
        else:
            self.away_since = self.away_since or now
            if now - self.away_since >= (PINNED_LEAVE_DELAY if self.pinned else LEAVE_DELAY):
                self._hide_card()

    # ------------------------------------------------------------ building blocks

    def _pal(self):
        return PALETTES[system_theme()]

    def _label(self, parent, text, font="body", fg="ink", **kw):
        p = self.pal
        return self.tk.Label(parent, text=text, font=self.f[font], fg=p[fg], bg=kw.pop("bg", p["surface"]),
                             anchor="w", justify="left", **kw)

    def _bar(self, parent, width, pct, sev, height=6, fill=None, track=None):
        p = self.pal
        h = self.px(height)
        cv = self.tk.Canvas(parent, width=width, height=h, bg=p["surface"], highlightthickness=0, bd=0)
        r = h / 2
        if sev == "neutral":
            fill, track = fill or p["ink2"], track or p["idle"]
        track = track or (p["track"][sev] if pct is not None else p["idle"])
        cv.create_line(r, r, width - r, r, fill=track, width=h, capstyle="round")
        if pct:
            end = r + (width - 2 * r) * min(pct, 100) / 100
            cv.create_line(r, r, max(end, r + 0.1), r, fill=fill or STATUS[sev], width=h, capstyle="round")
        return cv

    def _rows(self, parent, width):
        st, cfg = self.monitor.status, self.monitor.cfg
        for i, row in enumerate(limit_rows(st, time.time(), self.monitor.tz, cfg["yellow_at"], cfg["red_at"])):
            if i:
                self.tk.Frame(parent, height=1, bg=self.pal["stroke"]).pack(fill="x")
            f = self.tk.Frame(parent, bg=self.pal["surface"])
            f.pack(fill="x", pady=(self.px(8), self.px(8)))
            top = self.tk.Frame(f, bg=self.pal["surface"])
            top.pack(fill="x")
            self._label(top, row["name"], "name").pack(side="left")
            self._label(top, row["value"], "body", "ink2").pack(side="right")
            self._bar(f, width, row["pct"], row["sev"]).pack(fill="x", pady=(self.px(6), self.px(4)))
            self._label(f, row["sub"], "small", "ink3").pack(fill="x")

    def _live_line(self, parent):
        st = self.monitor.status
        text = (f"Largest live context: {est.fmt_tokens(st.live_context)} in {st.live_folder}"
                if st.live else "No live conversations in the last 30 minutes")
        return self._label(parent, text, "small", "ink3")

    # ------------------------------------------------------------ hover card

    def _show_card(self, pinned: bool) -> None:
        if self.card is None:
            self.pal = self._pal()
            tk = self.tk
            self.card = tk.Toplevel(self.root, bg=self.pal["surface"])
            self.card.overrideredirect(True)
            self.card.attributes("-topmost", True)
            self.card_body = tk.Frame(self.card, bg=self.pal["surface"])
            self.card_body.pack(fill="both", padx=self.px(16), pady=(self.px(14), self.px(12)))
            self._fill_card()
        self.pinned = pinned
        self.away_since = None
        self._place_card()
        self.card.deiconify()
        style_window(self.card, system_theme() == "dark", border=self.pal["stroke"])

    def _fill_card(self) -> None:
        for w in self.card_body.winfo_children():
            w.destroy()
        p, body = self.pal, self.card_body
        head = self.tk.Frame(body, bg=p["surface"])
        head.pack(fill="x", pady=(0, self.px(2)))
        self._label(head, "Claude usage", "head").pack(side="left")
        st = self.monitor.status
        tag = " estimate " if not st.exact else (f" {st.exact.plan.upper()} " if st.exact.plan else "")
        if tag:
            self._label(head, tag, "tag", "ink2", highlightthickness=1,
                        highlightbackground=p["stroke"]).pack(side="right")
        self._rows(body, self.px(306))
        self.tk.Frame(body, height=1, bg=p["stroke"]).pack(fill="x", pady=(self.px(2), self.px(8)))
        self._live_line(body).pack(fill="x")
        stack = [body]  # clicking anywhere on the card opens the details
        while stack:
            w = stack.pop()
            w.bind("<Button-1>", lambda e: self._card_clicked())
            stack.extend(w.winfo_children())

    def _place_card(self) -> None:
        c = self.card
        c.update_idletasks()
        w, h = c.winfo_reqwidth(), c.winfo_reqheight()
        rect = self.icon_rect() if self.icon_rect else None
        if rect is None:
            x, y = cursor_pos()
            rect = (x, y, x + 1, y + 1)
        left, top, right, bottom = work_area((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        gap = self.px(12)
        x = min(max((rect[0] + rect[2]) // 2 - w // 2, left + gap), right - w - gap)
        y = rect[1] - h - gap
        if y < top:  # taskbar at the top of the screen
            y = rect[3] + gap
        y = min(max(y, top + gap), bottom - h - gap)
        c.geometry(f"+{x}+{y}")

    def _card_clicked(self) -> None:
        c = self.card
        anchor = (c.winfo_rootx(), c.winfo_rooty(), c.winfo_rootx() + c.winfo_width(),
                  c.winfo_rooty() + c.winfo_height())
        self._hide_card()
        self._show_details(anchor)

    def _hide_card(self) -> None:
        if self.card is not None:
            self.card.destroy()
            self.card = None
        self.hover_since = None

    # ------------------------------------------------------------ details window

    def _details_anchor(self):
        """Where the details window opens from when there is no card: the tray icon, else the cursor."""
        rect = self.icon_rect() if self.icon_rect else None
        if rect:
            return rect[0], rect[1] - self.px(12), rect[2], rect[1] - self.px(12)
        x, y = cursor_pos()
        return x, y, x, y

    def _show_details(self, anchor=None) -> None:
        if self.details is not None and self.details.winfo_exists():
            self.details.deiconify()
            self.details.lift()
            self.details.focus_force()
            return
        tk = self.tk
        self.pal = p = self._pal()
        win = self.details = tk.Toplevel(self.root, bg=p["surface"])
        win.title("Claude usage")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._close_details)
        win.attributes("-alpha", 0.0)  # build and measure invisibly, then place and show: no jump
        outer = tk.Frame(win, bg=p["surface"])
        outer.pack(fill="both", padx=self.px(20), pady=(self.px(8), self.px(16)))
        self.d_width = self.px(520)

        self.d_rows = tk.Frame(outer, bg=p["surface"], width=self.d_width)
        self.d_rows.pack(fill="x")
        self._label(outer, "This session", "name").pack(fill="x", pady=(self.px(14), self.px(4)))
        self.d_stats = tk.Frame(outer, bg=p["surface"])
        self.d_stats.pack(fill="x")
        self._label(outer, "Live conversations", "name").pack(fill="x", pady=(self.px(14), self.px(4)))
        self.d_live = tk.Frame(outer, bg=p["surface"])
        self.d_live.pack(fill="x")

        tk.Frame(outer, height=1, bg=p["stroke"]).pack(fill="x", pady=(self.px(10), self.px(10)))
        foot = tk.Frame(outer, bg=p["surface"])
        foot.pack(fill="x")
        self.d_updated = self._label(foot, "", "small", "ink3")
        self.d_updated.pack(side="left")
        self._button(foot, "Refresh", self._refresh_now).pack(side="right")
        self._fill_details()
        style_window(win, system_theme() == "dark")
        self._place_details(anchor or self._details_anchor())
        win.attributes("-alpha", 1.0)
        win.lift()
        win.focus_force()

    def _place_details(self, anchor) -> None:
        win = self.details
        win.update()  # map it now (still invisible), or Windows' default placement wins afterwards
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(win.winfo_id(), 2)
        # Outer size includes the title bar and borders, which Tk's own sizes leave out.
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        size = (r.right - r.left, r.bottom - r.top)
        work = work_area((anchor[0] + anchor[2]) // 2, (anchor[1] + anchor[3]) // 2)
        x, y = place_near(anchor, size, work, self.px(12))
        user32.SetWindowPos(hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE | NOZORDER | NOACTIVATE

    def _set_window_icon(self) -> None:
        """Taskbar and title-bar icon: the same ring as the tray, at the current usage."""
        import base64, io
        from .icon import lock_icon, ring_icon
        st, cfg = self.monitor.status, self.monitor.cfg
        img = lock_icon(st.reset_at - time.time()) if st.locked else ring_icon(st.pct, cfg["yellow_at"], cfg["red_at"])
        buf = io.BytesIO()
        img.save(buf, "PNG")
        self.d_icon = self.tk.PhotoImage(data=base64.b64encode(buf.getvalue()))  # keep a reference
        self.details.iconphoto(False, self.d_icon)

    def _button(self, parent, text, command):
        p = self.pal
        b = self.tk.Button(parent, text=text, command=command, font=self.f["body"], bg=p["surface2"], fg=p["ink"],
                           activebackground=p["idle"], activeforeground=p["ink"], relief="flat", bd=0,
                           highlightthickness=1, highlightbackground=p["stroke"], padx=self.px(12),
                           pady=self.px(4), cursor="hand2")
        b.bind("<Enter>", lambda e: b.configure(bg=p["idle"]))
        b.bind("<Leave>", lambda e: b.configure(bg=p["surface2"]))
        return b

    def _fill_details(self) -> None:
        if self.details is None or not self.details.winfo_exists():
            return
        p, st, tz = self.pal, self.monitor.status, self.monitor.tz
        for frame in (self.d_rows, self.d_stats, self.d_live):
            for w in frame.winfo_children():
                w.destroy()
        self._rows(self.d_rows, self.d_width)

        wt = st.window_tokens
        cells = (("Output", wt["output"]), ("Input + cache write", wt["input"] + wt["cache_write"]),
                 ("Cache read", wt["cache_read"]), ("Today, all tokens", st.today_tokens))
        for i, (k, v) in enumerate(cells):
            cell = self.tk.Frame(self.d_stats, bg=p["surface"])
            cell.grid(row=0, column=i, sticky="w", padx=(0, self.px(28)))
            self._label(cell, k, "small", "ink3").pack(anchor="w")
            self._label(cell, est.fmt_tokens(v), "stat").pack(anchor="w")

        if not st.live:
            self._label(self.d_live, "None in the last 30 minutes", "small", "ink3").pack(fill="x")
        top = st.live[0][1] if st.live else 1
        for i, (folder, ctx) in enumerate(st.live[:4]):
            self._label(self.d_live, folder, "body").grid(row=i, column=0, sticky="w", pady=self.px(2))
            self._bar(self.d_live, self.px(180), ctx / top * 100, "good", height=4, fill=p["ink3"],
                      track=p["idle"]).grid(row=i, column=1, padx=(self.px(16), self.px(16)))
            self._label(self.d_live, est.fmt_tokens(ctx), "body", "ink2").grid(row=i, column=2, sticky="e")
        self.d_live.grid_columnconfigure(0, weight=1)

        cfg = self.monitor.cfg
        self._set_window_icon()
        self.d_updated.configure(text=footer_text(st, self.monitor.updated, tz, cfg["poll_seconds"], cfg["api_seconds"]))

    def _refresh_now(self) -> None:
        if self.refresh_cb:
            threading.Thread(target=self.refresh_cb, daemon=True).start()

    def _close_details(self) -> None:
        self.details.destroy()
        self.details = None
