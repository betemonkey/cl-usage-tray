import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from usage_tray import estimator as est
from usage_tray.app import Monitor
from usage_tray.config import DEFAULTS
from usage_tray.parser import Call, Lockout, LogStore, parse_ts

H = 3600
T0 = parse_ts("2026-10-04T10:03:20Z")
OPUS = "claude-opus-5-5"
FIXTURES = Path(__file__).parent / "fixtures" / "projects"


def call(ts, out=0, cr=0, cw=0, inp=0, model=OPUS, file="f1", cwd="C:\\work\\alpha", id=None):
    return Call(id or f"m{ts}", ts, model, inp, out, cr, cw, file, cwd)


# ---- windows

def test_window_starts_at_first_call_floored_to_10_minutes():
    start = parse_ts("2026-10-04T10:00:00Z")
    assert est.find_window([T0], [], T0 + 60) == (start, start + 5 * H)


def test_next_window_starts_at_first_call_after_previous_ended():
    times = [T0, T0 + 4 * H, T0 + 5 * H + 30 * 60]  # last call is after 15:00
    start, end = est.find_window(times, [], T0 + 6 * H)
    assert start == parse_ts("2026-10-04T15:30:00Z")
    assert end - start == 5 * H


def test_idle_after_window_end_gives_none():
    assert est.find_window([T0], [], T0 + 6 * H) is None


def test_lockout_pins_window():
    reset = parse_ts("2026-10-04T14:10:00Z")
    lk = Lockout(ts=reset - H, resets_at=reset)
    assert est.find_window([T0], [lk], T0 + 60) == (reset - 5 * H, reset)


# ---- costs and fit

def test_price_prefix_longest_match():
    assert est.price_for("claude-opus-5-5", est.DEFAULT_PRICES)["input"] == 4
    assert est.price_for("claude-opus-5", est.DEFAULT_PRICES)["input"] == 5
    assert est.price_for("claude-haiku-4-5-20251001", est.DEFAULT_PRICES)["output"] == 5
    assert est.price_for("something-new", est.DEFAULT_PRICES) == est.FALLBACK_PRICE


def test_features_weight_by_model_price():
    f = est.features(est.model_tokens([call(0, out=1_000_000), call(1, out=1_000_000, model="claude-haiku-4-5")]),
                     est.DEFAULT_PRICES)
    assert f["output"] == pytest.approx(20 + 5)


def sample(day, out, cr):
    reset = T0 + day * 86400
    return est.Sample(reset, reset - H, {OPUS: {"input": 0, "output": out, "cache_read": cr, "cache_write": 0}})


def test_fit_puts_lockouts_near_100():
    # Output costs $20/M and cache read $0.2/M: each lockout is ~$100.
    samples = [sample(0, 4_000_000, 100_000_000), sample(1, 3_000_000, 200_000_000),
               sample(2, 4_500_000, 50_000_000)]
    cal = est.fit(samples, est.DEFAULT_PRICES, now=T0 + 3 * 86400)
    assert cal.samples == 3
    assert cal.limit == pytest.approx(100, rel=0.05)
    assert all(c == pytest.approx(100, rel=0.05) for c in cal.costs)
    assert cal.spread < 0.05


def test_fit_finds_cheaper_cache_reads():
    # The limit behaves as if cache reads cost a quarter of list price.
    samples = [sample(0, 5_000_000 - 1_000_000 * k, 100_000_000 * k) for k in (0, 1, 2, 3)]
    for s, k in zip(samples, (0, 1, 2, 3)):
        s.tokens[OPUS]["cache_read"] = 4 * 100_000_000 * k
    cal = est.fit(samples, est.DEFAULT_PRICES, now=T0 + 5 * 86400)
    assert cal.mult["cache_read"] == 0.25
    assert cal.spread < 0.01


def test_fit_without_lockouts_uses_default():
    assert est.fit([], est.DEFAULT_PRICES, now=T0).limit == est.Calibration().limit


# ---- status

def test_status_percent_and_live_context():
    cal = est.Calibration(limit=10.0)
    calls = [call(T0, out=250_000, cr=100_000, file="a"),            # $5 + $0.02
             call(T0 + 60, cr=500_000, cw=20_000, file="b", cwd="C:\\work\\beta")]
    st = est.compute_status(calls, [], cal, est.DEFAULT_PRICES, now=T0 + 120)
    assert st.pct == pytest.approx((5 + 0.02 + 0.1 + 0.16) / 10 * 100)
    assert st.live_context == 520_000 and st.live_folder == "beta"
    assert st.live == [("beta", 520_000), ("alpha", 100_000)]
    assert st.reset_at - st.window_start == 5 * H
    assert not st.locked


def test_live_context_uses_latest_call_per_conversation_and_ignores_stale():
    calls = [call(T0, cr=900_000, file="a"), call(T0 + 60, cr=100_000, file="a"),
             call(T0 - 2 * H, cr=5_000_000, file="old")]
    st = est.compute_status(calls, [], est.Calibration(), est.DEFAULT_PRICES, now=T0 + 120)
    assert st.live_context == 100_000


def test_locked_until_reset_unless_calls_succeed_after():
    reset = T0 + 2 * H
    lk = Lockout(ts=T0, resets_at=reset)
    calls = [call(T0 - 60, out=10)]
    st = est.compute_status(calls, [lk], est.Calibration(), est.DEFAULT_PRICES, now=T0 + 60)
    assert st.locked and st.reset_at == reset
    assert "LOCKED" in est.tooltip(st, T0 + 60)
    # A successful call after the rejection (e.g. another account) means not locked.
    calls.append(call(T0 + 30, out=10))
    assert not est.compute_status(calls, [lk], est.Calibration(), est.DEFAULT_PRICES, now=T0 + 60).locked
    # After the reset it is over.
    assert not est.compute_status(calls[:1], [lk], est.Calibration(), est.DEFAULT_PRICES, now=reset + 1).locked


def test_today_total_uses_timezone():
    tz = ZoneInfo("Europe/Brussels")
    midnight = parse_ts("2026-10-03T22:00:00Z")  # 00:00 in Brussels (CEST)
    calls = [call(midnight - 60, out=1000), call(midnight + 60, out=7)]
    st = est.compute_status(calls, [], est.Calibration(), est.DEFAULT_PRICES, now=midnight + 120, tz=tz)
    assert st.today_tokens == 7


def test_tooltip_is_short_and_labelled_estimate():
    st = est.Status(pct=42, window_start=T0, reset_at=T0 + 5 * H, today_tokens=10**9,
                    live=[("x" * 80, 999_999)])
    text = est.tooltip(st, T0)
    assert len(text) <= 127
    assert "estimate" in text


# ---- end to end on fixtures

def test_monitor_on_fixture_logs(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "projects")
    cfg = {**DEFAULTS, "timezone": "UTC", "live_api": False}
    mon = Monitor(cfg, store=LogStore(tmp_path / "projects"), samples={}, persist=False)
    st = mon.refresh(now=parse_ts("2026-10-04T09:30:00Z"))
    assert st.locked
    assert st.reset_at == 1791108000
    assert list(mon.samples) == [1791108000]
    assert set(mon.samples[1791108000].tokens) == {OPUS, "claude-haiku-4-5-20251001"}
    assert st.window_tokens["output"] == 400 + 600 + 200


# ---- weekly limits and claude.ai readings

BRU = ZoneInfo("Europe/Brussels")


def test_week_start_is_last_monday_8am_local():
    sat = parse_ts("2026-10-03T12:00:00Z")
    assert est.week_start(sat, BRU) == parse_ts("2026-09-28T06:00:00Z")  # Monday 08:00 CEST
    mon_early = parse_ts("2026-09-28T05:00:00Z")  # Monday 07:00 local, before the reset
    assert est.week_start(mon_early, BRU) == parse_ts("2026-09-21T06:00:00Z")
    after_dst = parse_ts("2026-10-27T12:00:00Z")  # Tuesday after the switch to CET
    assert est.week_start(after_dst, BRU) == parse_ts("2026-10-26T07:00:00Z")


def test_limit_from_reading():
    assert est.limit_from_reading(30.0, 60) == pytest.approx(50.0)
    assert est.limit_from_reading(30.0, 1) is None  # too little to scale from
    assert est.limit_from_reading(0.0, 50) is None


def test_weekly_and_fable_pct_from_limits():
    now = parse_ts("2026-10-03T12:00:00Z")
    calls = [call(now - 3600, out=1_000_000),                                         # opus $20
             call(now - 7200, out=1_000_000, model="claude-fable-5-1"),               # fable $50
             call(parse_ts("2026-09-27T12:00:00Z"), out=9_000_000)]                   # last week
    st = est.compute_status(calls, [], est.Calibration(), est.DEFAULT_PRICES, now, BRU,
                            week_limits={"week": 140.0})
    assert st.week.cost == pytest.approx(70) and st.week.pct == pytest.approx(50)
    assert st.fable.cost == pytest.approx(50) and st.fable.pct is None  # not matched yet
    assert st.week.reset_at == parse_ts("2026-10-05T06:00:00Z")


def test_monitor_auto_calibrates_from_live_reading(tmp_path, monkeypatch):
    from usage_tray import live
    shutil.copytree(FIXTURES, tmp_path / "projects")
    now = parse_ts("2026-10-04T08:30:00Z")
    reading = live.LiveUsage(fetched_at=now, session=live.Limit("Current session", 50, None),
                             weekly=[live.Limit("This week", 20, None)])
    monkeypatch.setattr(live, "get_usage", lambda now=None: reading)
    mon = Monitor({**DEFAULTS, "timezone": "UTC"}, store=LogStore(tmp_path / "projects"), samples={},
                  readings={}, persist=False)
    st = mon.refresh(now=now)
    assert st.pct == 50 and st.exact is reading
    assert set(mon.readings) == {"session", "week"}  # 50% and 20% are both precise enough
    # With the endpoint gone, the estimate lands where the last reading was.
    mon.exact, mon.cfg["live_api"] = None, False
    st = mon.refresh(now=now)
    assert st.exact is None and st.pct == pytest.approx(50)
    assert st.week.pct == pytest.approx(20)


def test_card_rows():
    from usage_tray.ui import limit_rows
    st = est.Status(pct=64, window_start=T0, reset_at=T0 + 2 * H,
                    week=est.Weekly(reset_at=T0 + 86400, cost=10, pct=88), fable=est.Weekly(reset_at=T0 + 86400))
    rows = limit_rows(st, T0)
    assert [r["name"] for r in rows] == ["Current session", "This week", "Fable this week"]
    assert rows[0]["value"] == "64% used" and rows[0]["sev"] == "warn" and "in 2h 00m" in rows[0]["sub"]
    assert rows[1]["sev"] == "crit"
    assert rows[2]["pct"] is None and rows[2]["value"] == "Not set"
    locked = limit_rows(est.Status(locked=True, reset_at=T0 + 600), T0)
    assert locked[0]["value"] == "Locked" and locked[0]["sev"] == "crit"
