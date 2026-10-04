import shutil
from pathlib import Path

from usage_tray.parser import Call, Lockout, LogStore, parse_line, parse_ts

FIXTURES = Path(__file__).parent / "fixtures" / "projects"


def test_parse_line_kinds():
    assert parse_line('{"type":"user","timestamp":"2026-10-04T08:00:00Z"}') is None
    assert parse_line("not json, but mentions \"usage\"") is None
    synthetic = ('{"type":"assistant","timestamp":"2026-10-04T08:00:00Z","message":{"id":"x",'
                 '"model":"<synthetic>","usage":{"input_tokens":0}}}')
    assert parse_line(synthetic) is None


def test_store_reads_fixtures_and_dedupes(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES, root)
    store = LogStore(root)
    store.poll(0)

    assert set(store.calls) == {"msg_A", "msg_B", "msg_C"}  # partial last line not parsed yet
    a = store.calls["msg_A"]
    assert a.output == 400  # repeated id: largest counts kept
    assert a.ts == parse_ts("2026-10-04T08:00:05.000Z")  # first timestamp kept
    assert a.context == 2 + 30000 + 5000
    assert store.calls["msg_C"].model.startswith("claude-haiku")
    assert "subagents" in store.calls["msg_C"].file

    (lk,) = store.lockouts.values()
    assert lk.resets_at == 1791108000
    assert lk.ts == parse_ts("2026-10-04T09:00:00.000Z")
    assert lk.last_ts == parse_ts("2026-10-04T09:01:00.000Z")


def test_incremental_poll_finishes_partial_line(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES, root)
    store = LogStore(root)
    store.poll(0)
    f = root / "C--work-alpha" / "sess1.jsonl"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write('ens":3,"output_tokens":7,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}},'
                 '"cwd":"C:\\\\work\\\\alpha"}\n')
    read = store.poll(0)
    assert "msg_PARTIAL" in store.calls
    assert store.calls["msg_PARTIAL"].output == 7
    assert read < f.stat().st_size  # only the new bytes were read
    assert store.poll(0) == 0  # nothing new


def test_poll_skips_old_files(tmp_path):
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES, root)
    store = LogStore(root)
    store.poll(since=4102444800)  # year 2100: every file is "old"
    assert not store.calls


def test_prune_and_lockout_merge():
    store = LogStore(Path("nowhere"))
    store.add(Call("a", 100, "m", 0, 1, 0, 0, "f", ""))
    store.add(Call("b", 200, "m", 0, 1, 0, 0, "f", ""))
    store.prune(150)
    assert list(store.calls) == ["b"]
    store.add(Lockout(ts=50, resets_at=999))
    store.add(Lockout(ts=40, resets_at=999))
    store.add(Lockout(ts=60, resets_at=999))
    lk = store.lockouts[999]
    assert (lk.ts, lk.last_ts) == (40, 60)
