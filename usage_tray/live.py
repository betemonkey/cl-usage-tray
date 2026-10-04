"""Exact plan usage from Anthropic's OAuth usage endpoint, the one Claude Code's /usage screen calls
(same approach as clippyred's usage.py).

The endpoint is undocumented, so parsing is defensive: anything unrecognised is skipped and the app
falls back to its local estimate. The token is only read (never refreshed or written) and only sent
to api.anthropic.com.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

CREDS_PATH = Path(os.environ.get("USERPROFILE", Path.home())) / ".claude" / ".credentials.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT = 10


class LiveError(Exception):
    """A short, user-facing reason why live data is unavailable."""


@dataclass
class Limit:
    name: str
    pct: float
    resets_at: float | None
    severity: str = "normal"  # endpoint's own: normal, warning, ...


@dataclass
class Credit:
    used: float
    limit: float
    resets_at: float | None

    @property
    def left(self) -> float:
        return max(0.0, self.limit - self.used)


@dataclass
class LiveUsage:
    fetched_at: float
    plan: str = ""
    session: Limit | None = None
    weekly: list = field(default_factory=list)  # [Limit]: all models first, then per-model limits
    credit: Credit | None = None


def load_token(path: Path = CREDS_PATH, now: float | None = None) -> tuple[str, str]:
    """(access token, plan) from Claude Code's credentials. Read-only."""
    try:
        oauth = json.loads(path.read_text(encoding="utf-8-sig"))["claudeAiOauth"]
        token = oauth["accessToken"]
    except FileNotFoundError:
        raise LiveError("Not logged in to Claude Code")
    except (OSError, ValueError, KeyError, TypeError):
        raise LiveError("Claude Code credentials unreadable")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 <= (now or time.time()) + 60:
        raise LiveError("Claude Code login expired; it renews the next time you use Claude Code")
    return token, str(oauth.get("subscriptionType") or "")


def fetch(token: str) -> dict:
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise LiveError("Usage endpoint rejected the login; it renews the next time you use Claude Code")
        if e.code == 429:
            raise LiveError("Usage endpoint is rate limiting; retrying later")
        raise LiveError(f"Usage endpoint error (HTTP {e.code})")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise LiveError("Can't reach api.anthropic.com")
    except ValueError:
        raise LiveError("Unexpected reply from the usage endpoint")


def _ts(value) -> float | None:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _pct(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse(payload: dict, plan: str = "", now: float | None = None) -> LiveUsage:
    """Normalise the reply. The `limits` list is preferred (it has per-model weekly limits);
    the older top-level windows are the fallback."""
    if not isinstance(payload, dict):
        raise LiveError("Unexpected reply from the usage endpoint")
    out = LiveUsage(fetched_at=now or time.time(), plan=plan)
    for lim in payload.get("limits") or []:
        if not isinstance(lim, dict) or _pct(lim.get("percent")) is None:
            continue
        kind, group = str(lim.get("kind", "")), str(lim.get("group", ""))
        item = Limit("", _pct(lim["percent"]), _ts(lim.get("resets_at")), str(lim.get("severity") or "normal"))
        if kind == "session" or group == "session":
            item.name = "Current session"
            out.session = item
        elif group == "weekly":
            scope = lim.get("scope") if isinstance(lim.get("scope"), dict) else {}
            model = (scope.get("model") or {}).get("display_name") if isinstance(scope.get("model"), dict) else None
            item.name = f"{model} this week" if model else "This week"
            out.weekly.append(item)
    if out.session is None:
        win = payload.get("five_hour")
        if isinstance(win, dict) and _pct(win.get("utilization")) is not None:
            out.session = Limit("Current session", _pct(win["utilization"]), _ts(win.get("resets_at")))
    if not out.weekly:
        win = payload.get("seven_day")
        if isinstance(win, dict) and _pct(win.get("utilization")) is not None:
            out.weekly.append(Limit("This week", _pct(win["utilization"]), _ts(win.get("resets_at"))))
    out.weekly.sort(key=lambda l: l.name != "This week")  # all-models limit first
    # Credits: the window that carries dollar amounts (its key is an internal code name).
    for value in payload.values():
        if isinstance(value, dict) and _pct(value.get("limit_dollars")) and _pct(value.get("used_dollars")) is not None:
            out.credit = Credit(_pct(value["used_dollars"]), _pct(value["limit_dollars"]), _ts(value.get("resets_at")))
            break
    if out.session is None and not out.weekly:
        raise LiveError("Unexpected reply from the usage endpoint")
    return out


def get_usage(now: float | None = None) -> LiveUsage:
    token, plan = load_token(now=now)
    return parse(fetch(token), plan, now)
