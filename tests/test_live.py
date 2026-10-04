import json
from pathlib import Path

import pytest

from usage_tray import live
from usage_tray.parser import parse_ts

REPLY = json.loads((Path(__file__).parent / "fixtures" / "usage_reply.json").read_text())


def test_parse_limits_list():
    u = live.parse(REPLY, "max", now=1.0)
    assert u.session.pct == 5 and u.session.resets_at == pytest.approx(parse_ts("2026-10-04T23:59:59.598757Z"))
    assert [(w.name, w.pct) for w in u.weekly] == [("This week", 61), ("Fable this week", 88)]
    assert u.weekly[1].severity == "warning"
    assert u.credit.limit == 250 and u.credit.left == pytest.approx(160.251303)
    assert u.plan == "max"


def test_parse_falls_back_to_window_dicts():
    old = {k: v for k, v in REPLY.items() if k != "limits"}
    u = live.parse(old)
    assert u.session.pct == 5 and [w.pct for w in u.weekly] == [61]


def test_parse_rejects_unknown_shape():
    with pytest.raises(live.LiveError):
        live.parse({"something": "else"})


def test_load_token(tmp_path):
    creds = tmp_path / "creds.json"
    with pytest.raises(live.LiveError, match="Not logged in"):
        live.load_token(creds)
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 2_000_000, "subscriptionType": "max"}}))
    assert live.load_token(creds, now=1_000) == ("t", "max")
    with pytest.raises(live.LiveError, match="expired"):
        live.load_token(creds, now=2_000)


def test_card_rows_use_live_numbers():
    from usage_tray import estimator as est
    from usage_tray.ui import footer_text, limit_rows
    u = live.parse(REPLY, "max", now=parse_ts("2026-10-04T20:00:00Z"))
    st = est.Status(pct=5, reset_at=u.session.resets_at, exact=u)
    rows = limit_rows(st, now=parse_ts("2026-10-04T20:00:00Z"))
    assert [r["name"] for r in rows] == ["Current session", "This week", "Fable this week", "Cloud session credits"]
    assert rows[2]["sev"] == "crit" and rows[2]["sub"].startswith("Separate Fable limit, resets Monday")
    assert rows[3]["value"] == "$160 of $250 left" and rows[3]["sev"] == "neutral"
    assert footer_text(st, 0, None, 30, 120).startswith("Live from Anthropic")


def test_details_opens_where_the_card_was():
    from usage_tray.ui import place_near
    work = (0, 0, 2560, 1380)
    card = (2100, 900, 2440, 1368)  # card above the tray, bottom-right of the screen
    assert place_near(card, (720, 760), work) == (2440 - 720, 1368 - 760)  # same right and bottom edge
    assert place_near(card, (720, 760), work)[0] + 720 <= 2560 - 12
    assert place_near((2500, 1300, 2560, 1380), (720, 760), work) == (2560 - 720 - 12, 1380 - 760 - 12)  # clamped
    assert place_near((10, 10, 50, 50), (720, 760), work) == (12, 12)  # never off the top-left either
