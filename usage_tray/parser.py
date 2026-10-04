"""Incremental, read-only reader for Claude Code transcript files (*.jsonl).

Only files modified inside the horizon are opened, and each file is read
from the byte offset where the previous poll stopped, so polling is cheap.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("USERPROFILE", Path.home())) / ".claude" / "projects"


@dataclass
class Call:
    """One assistant API call (de-duplicated by message.id)."""
    id: str
    ts: float  # epoch seconds
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    file: str
    cwd: str

    @property
    def context(self) -> int:
        """Prompt size of this call: what the conversation context was."""
        return self.input + self.cache_read + self.cache_write

    @property
    def tokens(self) -> int:
        return self.context + self.output


@dataclass
class Lockout:
    """A five-hour limit rejection: first and last rejection seen for one reset time."""
    ts: float
    resets_at: float
    last_ts: float = 0.0

    def __post_init__(self):
        self.last_ts = max(self.last_ts, self.ts)


def parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def parse_line(line: str, file: str = ""):
    """Return a Call, a Lockout, or None for one jsonl line."""
    if '"usage"' not in line and '"quotaLimits"' not in line:
        return None  # fast path: most lines are user/tool lines
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or "timestamp" not in d:
        return None
    q = d.get("quotaLimits")
    if isinstance(q, dict) and q.get("status") == "rejected" and q.get("rateLimitType") == "five_hour":
        return Lockout(ts=parse_ts(d["timestamp"]), resets_at=float(q["resetsAt"]))
    if d.get("type") != "assistant":
        return None
    m = d.get("message") or {}
    u = m.get("usage")
    mid = m.get("id")
    model = m.get("model") or ""
    if not u or not mid or model.startswith("<"):  # "<synthetic>" error lines carry no usage
        return None
    return Call(
        id=mid,
        ts=parse_ts(d["timestamp"]),
        model=model,
        input=u.get("input_tokens") or 0,
        output=u.get("output_tokens") or 0,
        cache_read=u.get("cache_read_input_tokens") or 0,
        cache_write=u.get("cache_creation_input_tokens") or 0,
        file=file,
        cwd=d.get("cwd") or "",
    )


class LogStore:
    """Keeps calls and lockouts from recently modified transcripts in memory."""

    def __init__(self, root: Path = DEFAULT_ROOT):
        self.root = Path(root)
        self.offsets: dict[str, int] = {}
        self.calls: dict[str, Call] = {}
        self.lockouts: dict[float, Lockout] = {}  # keyed by resets_at

    def add(self, item) -> None:
        if isinstance(item, Call):
            old = self.calls.get(item.id)
            if old is None:
                self.calls[item.id] = item
            else:  # same message logged once per content block; keep first ts, largest counts
                old.output = max(old.output, item.output)
                old.input = max(old.input, item.input)
                old.cache_read = max(old.cache_read, item.cache_read)
                old.cache_write = max(old.cache_write, item.cache_write)
        elif isinstance(item, Lockout):
            old = self.lockouts.get(item.resets_at)
            if old is None:
                self.lockouts[item.resets_at] = item
            else:
                old.ts = min(old.ts, item.ts)
                old.last_ts = max(old.last_ts, item.last_ts)

    def poll(self, since: float) -> int:
        """Read new lines from files modified at or after `since`. Returns bytes read."""
        read = 0
        if not self.root.is_dir():
            return 0
        for path in self.root.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < since:
                continue
            key = str(path)
            start = self.offsets.get(key, 0)
            if st.st_size < start:  # file was rewritten
                start = 0
            if st.st_size == start:
                continue
            read += self._read(path, key, start)
        return read

    def _read(self, path: Path, key: str, start: int) -> int:
        with open(path, "rb") as f:  # read-only
            f.seek(start)
            data = f.read()
        end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
        for raw in data[:end].splitlines():
            item = parse_line(raw.decode("utf-8", "replace"), key)
            if item is not None:
                self.add(item)
        self.offsets[key] = start + end
        return end

    def prune(self, before: float) -> None:
        """Drop calls older than `before` to bound memory."""
        self.calls = {k: c for k, c in self.calls.items() if c.ts >= before}

    def sorted_calls(self) -> list[Call]:
        return sorted(self.calls.values(), key=lambda c: c.ts)
