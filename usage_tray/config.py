"""User config and saved lockout history, kept in %LOCALAPPDATA%\\usage-tray (never in ~/.claude)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .estimator import DEFAULT_PRICES, Sample

APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "usage-tray"
CONFIG_FILE = APP_DIR / "config.json"
SAMPLES_FILE = APP_DIR / "lockouts.json"

DEFAULTS = {
    "poll_seconds": 30,
    "timezone": "Europe/Brussels",
    "recent_hours": 6,          # after the first full scan, only files modified this recently are polled
    "live_minutes": 30,         # a conversation counts as live if it had a call this recently
    "yellow_at": 60,
    "red_at": 85,
    "prices": DEFAULT_PRICES,   # $ per million tokens, by model-name prefix
    "multipliers": None,        # e.g. {"cache_read": 0.5} to pin them; null = auto-fit
    "limit": None,              # weighted cost that equals 100%; null = auto-fit
    "exclude_lockouts": [],     # resetsAt values (unix seconds) to leave out of the fit
}


def load_config(path: Path = CONFIG_FILE) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    prices = {**cfg["prices"], **(user.pop("prices", None) or {})}
    cfg.update(user)
    cfg["prices"] = prices
    return cfg


def load_samples(path: Path = SAMPLES_FILE) -> dict[float, Sample]:
    """Lockouts seen in earlier runs, so the fit survives transcript cleanup."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {float(d["resets_at"]): Sample(float(d["resets_at"]), float(d["rejected_at"]), d["tokens"])
            for d in raw}


def save_samples(samples: dict[float, Sample], path: Path = SAMPLES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [{"resets_at": s.resets_at, "rejected_at": s.rejected_at, "tokens": s.tokens}
            for s in sorted(samples.values(), key=lambda s: s.resets_at)]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)
