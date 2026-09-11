"""Tests for reserves.py: scheduled safety reserves."""

from __future__ import annotations

import pytest

from claude_swap.reserves import (
    Reserve,
    ReserveError,
    ReserveStore,
    make_reserve,
    normalize_window,
    parse_when,
)


class TestParsing:
    def test_normalize_window_aliases(self):
        assert normalize_window("5H") == "5h"
        assert normalize_window("seven_day") == "7d"
        assert normalize_window("weekly") == "7d"
        assert normalize_window("Fable") == "Fable"
        with pytest.raises(ReserveError):
            normalize_window("  ")

    def test_parse_when_forms(self):
        assert parse_when(None) is None
        assert parse_when("") is None
        d = parse_when("2026-09-16")
        dt = parse_when("2026-09-16 18:30")
        assert dt is not None and d is not None and dt > d
        with pytest.raises(ReserveError):
            parse_when("next tuesday")

    def test_make_reserve_validates(self):
        with pytest.raises(ReserveError):
            make_reserve(window="7d", pct=120)
        with pytest.raises(ReserveError):
            make_reserve(window="7d", pct=10, starts="2026-09-16", ends="2026-09-15")
        r = make_reserve(window="fable", pct=100, note="  deadline ")
        assert r.window == "fable" and r.is_blackout and r.note == "deadline"
        assert r.account is None

    def test_from_json_tolerates_garbage(self):
        assert Reserve.from_json({"id": "x"}) is None
        r = Reserve.from_json({"id": "x", "window": "5h", "pct": 250, "starts_at": "no"})
        assert r is not None and r.pct == 100.0 and r.starts_at is None


class TestEvaluation:
    def test_active_window(self):
        r = Reserve(id="a", window="7d", pct=40, starts_at=100.0, ends_at=200.0)
        assert not r.active_at(99.0)
        assert r.active_at(100.0)
        assert not r.active_at(200.0)
        assert r.expired_at(200.0)
        open_r = Reserve(id="b", window="7d", pct=40)
        assert open_r.active_at(0.0) and not open_r.expired_at(1e12)

    def test_effective_is_max_of_floor_and_active(self, tmp_path):
        store = ReserveStore(tmp_path)
        store.add(Reserve(id="a", window="7d", pct=40, ends_at=200.0))
        store.add(Reserve(id="b", window="7d", pct=25))
        store.add(Reserve(id="c", window="5h", pct=90))
        store.add(Reserve(id="d", window="7d", pct=60, account="other@x"))
        pct, src = store.effective("7d", now=150.0, email="me@x", floor=10.0)
        assert (pct, src.id) == (40.0, "a")
        pct, src = store.effective("7d", now=250.0, email="me@x", floor=10.0)
        assert (pct, src.id) == (25.0, "b")
        pct, src = store.effective("7d", now=250.0, email="me@x", floor=30.0)
        assert (pct, src) == (30.0, None)
        pct, src = store.effective("7d", now=250.0, email="other@x", floor=10.0)
        assert (pct, src.id) == (60.0, "d")
        pct, src = store.effective("Fable", now=250.0, email="me@x", floor=10.0)
        assert (pct, src) == (10.0, None)

    def test_account_scoped_entry_applies_to_unknown_email(self, tmp_path):
        store = ReserveStore(tmp_path)
        store.add(Reserve(id="d", window="7d", pct=60, account="other@x"))
        pct, _ = store.effective("7d", now=0.0, email=None, floor=0.0)
        assert pct == 60.0


class TestStore:
    def test_crud_and_purge(self, tmp_path):
        store = ReserveStore(tmp_path)
        r = store.add(make_reserve(window="7d", pct=40, ends="2020-01-01"))
        assert store.get(r.id[:4]).id == r.id
        store.update(r.id, pct=55.0)
        assert store.get(r.id).pct == 55.0
        assert store.purge_expired(now=4e9) == 1
        assert store.all() == []
        assert not store.remove(r.id)
        with pytest.raises(ReserveError):
            store.get("nope")

    def test_corrupt_file_reads_empty(self, tmp_path):
        store = ReserveStore(tmp_path)
        store.path.write_text("{not json")
        assert store.all() == []
