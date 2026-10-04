# usage-tray

Windows tray icon showing Claude usage against the 5-hour limit.

A ring in the notification area fills up with the **estimated** share of the
current 5-hour window: green below 60%, yellow 60–85%, red above 85%. When
you are locked out it turns into a purple disc showing the time until reset.

Everything is computed locally from Claude Code's transcripts. No network
access and no API keys. `~/.claude` is only ever read.

## Setup

```powershell
py -m pip install -r requirements.txt
pyw usage_tray.pyw          # pythonw: no console window
```

Right-click the icon to use the menu: **Refresh now**, **Details…**
(double-click also opens it), **Start with Windows** (a per-user `Run`
registry entry) and **Quit**. Only one instance runs at a time.

Tests: `py -m pip install pytest` then `py -m pytest`.

## What it shows

- **Tooltip** (three lines, because Windows caps tooltips at 127 characters):
  - the estimated % and the reset time (Europe/Brussels)
  - tokens used in this window and today
  - the largest live conversation context with its folder. That is the
    latest prompt size (input + cache read + cache write) of conversations
    that made a call in the last 30 minutes. Subagents count separately.
- **Details window**: window start and reset, the token breakdown, the
  weighted cost against the fitted limit, and the calibration quality.

## How the estimate works

The plan % is not in the logs, so it is estimated:

1. **Reading.** `%USERPROFILE%\.claude\projects\**\*.jsonl`, subagent files
   included. Assistant lines are de-duplicated by `message.id`. The first poll
   reads all history (about 600 MB in roughly 2 s, because lines without
   `"usage"` or `"quotaLimits"` are skipped before JSON parsing). After that,
   only files modified in the last 6 hours are polled, from where the last
   poll stopped.
2. **Windows.** Every logged lockout (`quotaLimits.status = "rejected"`,
   `rateLimitType = "five_hour"`) pins its window exactly, from `resetsAt - 5h`
   to `resetsAt`. Between lockouts, windows chain: a window starts at the first
   call after the previous one ended, rounded down to 10 minutes, and lasts
   5 hours. This matches every logged reset. For example, first call 00:51:08
   gives a reset at 05:50.
3. **Cost.** Each call costs tokens × the model's API list price for each
   category (input, output, cache read, cache write), times a multiplier per
   category.
4. **Calibration.** For each lockout, the cost from the window start to the
   first rejection should equal 100%. The multipliers are picked from a small
   grid to make those lockout costs as consistent as possible, with a mild
   pull toward list prices. 100% is then set at their recency-weighted
   median. The fit reruns whenever a new lockout appears. Lockout token
   totals are saved, so the calibration survives transcript cleanup.

The details window shows how well the past lockouts line up. With the data
as of Oct 2026 the spread is wide (about ±40%). The three most recent
lockouts agree with each other, but two earlier ones on Oct 3 came out much
lower. They have overlapping windows, which points to a second account,
possibly on another plan. Usage from claude.ai or other machines is
invisible to this app. Treat the number as a guide, not a gauge.

## Configuration

Optional: `%LOCALAPPDATA%\usage-tray\config.json`, for example

```json
{
  "poll_seconds": 30,
  "timezone": "Europe/Brussels",
  "yellow_at": 60,
  "red_at": 85,
  "multipliers": {"output": 1, "cache_read": 0.5, "cache_write": 1},
  "limit": null,
  "exclude_lockouts": [1791061800],
  "prices": {"claude-opus-5-5": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 8}}
}
```

- `multipliers` pins the weights instead of auto-fitting them.
- `limit` pins the weighted cost that counts as 100%.
- `exclude_lockouts` leaves out lockouts, for example ones from another
  account. Use their `resetsAt` values.
- `prices` are in $ per million tokens, matched by the longest model-name
  prefix.

The same folder holds `lockouts.json` (saved calibration data) and
`usage-tray.log`.

## Layout

```
usage_tray.pyw          entry point
usage_tray/parser.py    incremental jsonl reader (calls, lockouts)
usage_tray/estimator.py windows, weighted cost, fit, tooltip/details text
usage_tray/app.py       Monitor (refresh loop) and the pystray/Tk UI
usage_tray/icon.py      ring and lockout icons (Pillow)
usage_tray/config.py    config.json and saved lockouts
usage_tray/autostart.py Start-with-Windows toggle
tests/                  parser and estimator tests on fixture jsonl
mockups/                design mockups (option A, the ring, was chosen)
```
