"""Estimate how much of the 5-hour plan window has been used.

The real plan % is not in the logs. Instead every call gets a weighted cost
(token counts x per-model price x per-category multiplier), and the cost that
triggered past lockouts defines 100%. The multipliers and that limit are
fitted from the lockout history.
"""
from __future__ import annotations

import itertools
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .parser import Call, Lockout

WINDOW = 5 * 3600
START_ROUNDING = 600  # observed: a window starts at the first message, floored to 10 minutes
CATEGORIES = ("input", "output", "cache_read", "cache_write")

# API list prices in $ per million tokens; cache_write uses the 1-hour cache rate (2x input).
# Longest matching model-name prefix wins. Override in config.json.
DEFAULT_PRICES = {
    "claude-fable": {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 20},
    "claude-mythos": {"input": 10, "output": 50, "cache_read": 0.25, "cache_write": 20},
    "claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 8},
    "claude-opus": {"input": 5, "output": 25, "cache_read": 0.5, "cache_write": 10},
    "claude-sonnet-5": {"input": 2, "output": 10, "cache_read": 0.2, "cache_write": 4},
    "claude-sonnet": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 6},
    "claude-haiku": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 2},
}
FALLBACK_PRICE = DEFAULT_PRICES["claude-opus-5-5"]

# Search grid for the per-category multipliers (input is the fixed reference at 1).
GRID = (0.25, 0.5, 1.0, 2.0)
PRIOR = 0.02        # penalty per doubling away from list prices, keeps the fit near them
HALF_LIFE_DAYS = 3  # recent lockouts count more: the plan or account may have changed


def price_for(model: str, prices: dict) -> dict:
    keys = [k for k in prices if model.startswith(k)]
    return prices[max(keys, key=len)] if keys else FALLBACK_PRICE


def model_tokens(calls: list[Call]) -> dict:
    """Token counts per model and category: {model: {category: n}}."""
    out: dict = {}
    for c in calls:
        t = out.setdefault(c.model, dict.fromkeys(CATEGORIES, 0))
        for k in CATEGORIES:
            t[k] += getattr(c, k)
    return out


def features(tokens: dict, prices: dict) -> dict:
    """Dollar cost per token category at list prices (before multipliers)."""
    f = dict.fromkeys(CATEGORIES, 0.0)
    for model, t in tokens.items():
        p = price_for(model, prices)
        for k in CATEGORIES:
            f[k] += t[k] * p[k] / 1e6
    return f


def weighted(f: dict, mult: dict) -> float:
    return sum(f[k] * mult.get(k, 1.0) for k in CATEGORIES)


# ---------------------------------------------------------------- windows

def find_window(call_times: list[float], lockouts: list[Lockout], t: float):
    """Return (start, end) of the 5-hour window containing time t, or None if idle.

    Windows chain: each one starts at the first call after the previous one
    ended (floored to 10 minutes). A logged lockout pins its window exactly
    (resets_at - 5h .. resets_at), which also handles account switches.
    """
    anchors = sorted((lk.resets_at - WINDOW, lk.resets_at) for lk in lockouts)
    cur = None
    for ts in call_times:
        if ts > t:
            break
        anchor = next((a for a in reversed(anchors) if a[0] <= ts < a[1]), None)
        if anchor and (cur is None or anchor[0] > cur[0]):
            cur = anchor
        elif cur is None or ts >= cur[1]:
            start = ts - ts % START_ROUNDING
            cur = (start, start + WINDOW)
    anchor = next((a for a in reversed(anchors) if a[0] <= t < a[1]), None)
    if anchor and (cur is None or anchor[0] > cur[0]):
        cur = anchor
    if cur is None or t >= cur[1]:
        return None
    return cur


# ---------------------------------------------------------------- calibration

@dataclass
class Sample:
    """Tokens of one lockout window, from its start to the first rejection."""
    resets_at: float
    rejected_at: float
    tokens: dict  # {model: {category: n}}


@dataclass
class Calibration:
    limit: float = 150.0  # weighted cost that equals 100%; default from the Oct 2026 lockouts
    mult: dict = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 1.0))
    samples: int = 0
    spread: float = 0.0   # relative std-dev of the fitted lockout costs (0.1 = +/-10%)
    costs: list = field(default_factory=list)


def lockout_samples(calls: list[Call], lockouts: list[Lockout]) -> list[Sample]:
    out = []
    for lk in sorted(lockouts, key=lambda lk: lk.resets_at):
        start = lk.resets_at - WINDOW
        win = [c for c in calls if start <= c.ts <= lk.ts]
        if win:
            out.append(Sample(lk.resets_at, lk.ts, model_tokens(win)))
    return out


def _wmedian(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights))
    half, acc = sum(weights) / 2, 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0]


def fit(samples: list[Sample], prices: dict, now: float, fixed_mult: dict | None = None) -> Calibration:
    """Pick multipliers that make the lockout costs most consistent, then set 100%
    at their recency-weighted median."""
    if not samples:
        return Calibration()
    samples = sorted(samples, key=lambda s: s.resets_at)
    feats = [features(s.tokens, prices) for s in samples]
    weights = [0.5 ** ((now - s.rejected_at) / 86400 / HALF_LIFE_DAYS) for s in samples]

    def score(mult):
        costs = [weighted(f, mult) for f in feats]
        mean = sum(c * w for c, w in zip(costs, weights)) / sum(weights)
        if mean <= 0:
            return math.inf, costs
        var = sum(w * (c - mean) ** 2 for c, w in zip(costs, weights)) / sum(weights)
        penalty = PRIOR * sum(abs(math.log2(m)) for m in mult.values())
        return math.sqrt(var) / mean + penalty, costs

    if fixed_mult or len(samples) < 3:  # too few lockouts to fit multipliers: keep list prices
        candidates = [{**dict.fromkeys(CATEGORIES, 1.0), **(fixed_mult or {})}]
    else:
        candidates = [{"input": 1.0, "output": o, "cache_read": r, "cache_write": w}
                      for o, r, w in itertools.product(GRID, repeat=3)]
    best = min(candidates, key=lambda m: score(m)[0])
    costs = score(best)[1]
    limit = _wmedian(costs, weights)
    spread = statistics.pstdev(costs) / statistics.mean(costs) if len(costs) > 1 else 0.0
    return Calibration(limit=limit, mult=best, samples=len(samples), spread=spread,
                       costs=[c / limit * 100 for c in costs])


# ---------------------------------------------------------------- status

@dataclass
class Status:
    pct: float = 0.0
    window_start: float | None = None
    reset_at: float | None = None
    locked: bool = False
    window_tokens: dict = field(default_factory=lambda: dict.fromkeys(CATEGORIES, 0))
    window_cost: float = 0.0
    today_tokens: int = 0
    live_context: int = 0
    live_folder: str = ""
    calibration: Calibration = field(default_factory=Calibration)

    @property
    def window_total(self) -> int:
        return sum(self.window_tokens.values())


def local_midnight(now: float, tz) -> float:
    d = datetime.fromtimestamp(now, tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def compute_status(calls: list[Call], lockouts: list[Lockout], cal: Calibration, prices: dict,
                   now: float, tz=None, live_minutes: int = 30) -> Status:
    calls = sorted(calls, key=lambda c: c.ts)
    st = Status(calibration=cal)

    # Locked out: a rejection whose reset is still ahead, with no successful call since.
    for lk in sorted(lockouts, key=lambda lk: lk.resets_at, reverse=True):
        if lk.resets_at > now and not any(c.ts > lk.last_ts for c in calls):
            st.locked, st.pct = True, 100.0
            st.window_start, st.reset_at = lk.resets_at - WINDOW, lk.resets_at
            break

    if not st.locked:
        win = find_window([c.ts for c in calls], lockouts, now)
        if win:
            st.window_start, st.reset_at = win
    if st.window_start is not None:
        in_win = [c for c in calls if st.window_start <= c.ts <= now]
        for c in in_win:
            for k in CATEGORIES:
                st.window_tokens[k] += getattr(c, k)
        st.window_cost = weighted(features(model_tokens(in_win), prices), cal.mult)
        if not st.locked:
            st.pct = st.window_cost / cal.limit * 100 if cal.limit else 0.0

    midnight = local_midnight(now, tz)
    st.today_tokens = sum(c.tokens for c in calls if c.ts >= midnight)

    latest: dict[str, Call] = {}
    for c in calls:
        if c.ts >= now - live_minutes * 60:
            latest[c.file] = c  # calls are sorted, so this ends on each file's last call
    if latest:
        top = max(latest.values(), key=lambda c: c.context)
        st.live_context = top.context
        st.live_folder = os.path.basename(top.cwd.rstrip("\\/")) or Path(top.file).parent.name
    return st


# ---------------------------------------------------------------- formatting

def fmt_tokens(n: float) -> str:
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}k"
    return str(int(n))


def fmt_duration(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def fmt_clock(ts: float | None, tz=None) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "--:--"


def tooltip(st: Status, now: float, tz=None) -> str:
    """Three short lines; Windows caps tray tooltips at 127 characters."""
    if st.locked:
        head = f"Claude LOCKED, resets {fmt_clock(st.reset_at, tz)} (in {fmt_duration(st.reset_at - now)})"
    elif st.reset_at:
        head = f"Claude ~{st.pct:.0f}% of 5h (estimate), resets {fmt_clock(st.reset_at, tz)}"
    else:
        head = "Claude ~0% (estimate), no window running"
    lines = [head, f"Window {fmt_tokens(st.window_total)} tok, today {fmt_tokens(st.today_tokens)}"]
    if st.live_context:
        lines.append(f"Ctx {fmt_tokens(st.live_context)} {st.live_folder}")
    text = "\n".join(lines)
    return text if len(text) <= 127 else text[:126] + "…"


def details(st: Status, now: float, tz=None) -> list[tuple[str, str]]:
    cal = st.calibration
    w = st.window_tokens
    rows = [
        ("Estimated usage", "LOCKED OUT" if st.locked else f"~{st.pct:.0f}%"),
        ("Window started", fmt_clock(st.window_start, tz)),
        ("Resets", f"{fmt_clock(st.reset_at, tz)}" + (f" (in {fmt_duration(st.reset_at - now)})" if st.reset_at else "")),
        ("Output tokens", fmt_tokens(w["output"])),
        ("Input + cache write", fmt_tokens(w["input"] + w["cache_write"])),
        ("Cache read", fmt_tokens(w["cache_read"])),
        ("Weighted cost", f"{st.window_cost:.1f} of {cal.limit:.1f}"),
        ("Today total", f"{fmt_tokens(st.today_tokens)} tokens"),
        ("Largest live context", f"{fmt_tokens(st.live_context)} {st.live_folder}" if st.live_context else "none"),
        ("Calibration", f"{cal.samples} lockouts, spread ±{cal.spread * 100:.0f}%" if cal.samples else "default (no lockouts yet)"),
        ("Multipliers", ", ".join(f"{k} {v:g}" for k, v in cal.mult.items())),
    ]
    if cal.costs:
        rows.append(("Lockouts at (% of fit)", ", ".join(f"{c:.0f}" for c in cal.costs)))
    return rows

