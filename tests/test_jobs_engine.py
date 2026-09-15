"""Tests for jobs_engine.py: idle gate, capacity match, tick outcomes."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from claude_swap.jobs import Job, JobStore, new_job_id
from claude_swap.process_detection import ClaudeSession
from claude_swap.jobs_engine import (
    ErrorEvent,
    HoldEvent,
    JobsEngine,
    StartEvent,
    TickEvent,
    TickOutcome,
    interactive_idle,
)
from claude_swap.reserves import Reserve, ReserveStore
from claude_swap.settings import JobsSettings
from claude_swap.usage_store import UsageEntry
from tests.test_autoswitch import EngineHarness, FakeClock

NOW = 1_800_000_000.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _entry(five: float, seven: float, *, now: float, scoped: dict | None = None, age: float = 10.0) -> UsageEntry:
    lg = {
        "five_hour": {"pct": five, "resets_at": _iso(now + 4 * 3600)},
        "seven_day": {"pct": seven, "resets_at": _iso(now + 3 * 86400)},
    }
    if scoped:
        lg["scoped"] = [{"name": k, "pct": v, "resets_at": _iso(now + 3 * 86400)} for k, v in scoped.items()]
    return UsageEntry(last_good=lg, fetched_at=now - age, age_s=age)


def _session_file(root, pid: int, *, status: str, updated_ms: float, kind="interactive"):
    d = root / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": "s", "cwd": "/tmp/x", "startedAt": 0,
        "kind": kind, "entrypoint": "cli", "status": status, "statusUpdatedAt": updated_ms,
    }))


class TestIdle:
    def test_no_sessions_is_idle_forever(self, tmp_path):
        rep = interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=())
        assert rep.idle_s == float("inf")

    def test_busy_session_is_zero(self, tmp_path):
        _session_file(tmp_path, os.getpid(), status="busy", updated_ms=NOW * 1000)
        rep = interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=())
        assert rep.idle_s == 0.0 and len(rep.busy) == 1

    def test_idle_measured_from_status_stamp_across_roots(self, tmp_path):
        home = tmp_path / "home"
        prof = tmp_path / "prof"
        _session_file(home, os.getpid(), status="idle", updated_ms=(NOW - 1800) * 1000)
        _session_file(prof, os.getpid(), status="idle", updated_ms=(NOW - 600) * 1000)
        rep = interactive_idle(now=NOW, claude_home=home, profile_dirs=(prof,))
        assert rep.idle_s == 600.0

    def test_excluded_pids_and_non_interactive_ignored(self, tmp_path):
        _session_file(tmp_path, os.getpid(), status="busy", updated_ms=NOW * 1000)
        rep = interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=(), exclude_pids=frozenset({os.getpid()}))
        assert rep.idle_s == float("inf")
        _session_file(tmp_path, os.getpid(), status="busy", updated_ms=NOW * 1000, kind="bg")
        rep = interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=())
        assert rep.idle_s == float("inf")

    def test_unreadable_record_means_unknown(self, tmp_path):
        (tmp_path / "sessions").mkdir()
        (tmp_path / "sessions" / "1.json").write_text("{nope")
        rep = interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=())
        assert rep.idle_s is None and rep.unreadable == 1


@pytest.fixture
def harness(temp_home):
    h = EngineHarness(temp_home)
    h.seed(1, "a@example.com")
    h.seed(2, "b@example.com")
    h.make_live("a@example.com", 1)
    h.clock = FakeClock(NOW)
    return h


def _engine(harness, tmp_path, *, entries, settings=None, dry_run=False, **kw):
    events: list = []
    store = JobStore(harness.switcher.backup_dir)
    engine = JobsEngine(
        harness.switcher, settings or JobsSettings(quiet_minutes=10, reserve_pct=10, weekly_reserve_pct=10),
        events.append, store=store, clock=harness.clock, dry_run=dry_run,
        claude_home=tmp_path / "claude-home", **kw,
    )
    patcher = patch.object(harness.switcher, "usage_entries_by_account", return_value=entries)
    patcher.start()
    return engine, store, events, patcher


def _queue(store, folder, **kw) -> Job:
    base = dict(id=new_job_id(), name=kw.pop("name", "j"), folder=str(folder), prompt="p", estimate_pct=5.0)
    base.update(kw)
    return store.add(Job(**base))


class TestTick:
    def test_nothing_queued(self, harness, tmp_path):
        engine, store, events, p = _engine(harness, tmp_path, entries={})
        try:
            assert engine.tick() is TickOutcome.NOTHING
        finally:
            p.stop()
        assert isinstance(events[-1], TickEvent) and events[-1].queued == 0

    def test_hold_when_not_quiet(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(10, 10, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        _queue(store, tmp_path)
        _session_file(tmp_path / "claude-home", os.getpid(), status="busy", updated_ms=NOW * 1000)
        try:
            assert engine.tick() is TickOutcome.HELD
        finally:
            p.stop()
        assert isinstance(events[-1], HoldEvent) and events[-1].reason == "not-quiet"

    def test_hold_on_max_concurrent(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(10, 10, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        running = _queue(store, tmp_path, name="r")
        store.claim_for_run(running.id, os.getpid())
        _queue(store, tmp_path)
        try:
            assert engine.tick() is TickOutcome.HELD
        finally:
            p.stop()
        assert events[-1].reason == "max-concurrent"

    def test_start_picks_account_with_most_spare(self, harness, tmp_path):
        entries = {"1": _entry(60, 30, now=NOW), "2": _entry(10, 20, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True)
        job = _queue(store, tmp_path)
        try:
            assert engine.tick() is TickOutcome.STARTED
        finally:
            p.stop()
        start = events[-1]
        assert isinstance(start, StartEvent) and start.account == "b@example.com"
        assert store.get(job.id).state == "queued"  # dry-run never launches

    def test_real_start_calls_launch(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(80, 10, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        job = _queue(store, tmp_path)
        try:
            with patch.object(engine.runner, "launch", return_value=job) as launch:
                assert engine.tick() is TickOutcome.STARTED
        finally:
            p.stop()
        launch.assert_called_once()
        assert launch.call_args.kwargs["account"] == "1"

    def test_pinned_account_only(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(95, 10, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True)
        _queue(store, tmp_path, account="2")
        try:
            assert engine.tick() is TickOutcome.HELD
        finally:
            p.stop()
        assert events[-1].reason == "no-capacity"

    def test_reserve_blocks_start(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(10, 10, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True)
        engine.reserves.add(Reserve(id="b", window="7d", pct=100))
        _queue(store, tmp_path)
        try:
            assert engine.tick() is TickOutcome.HELD
        finally:
            p.stop()
        engine.reserves.remove("b")
        p2 = patch.object(harness.switcher, "usage_entries_by_account", return_value=entries)
        p2.start()
        try:
            assert engine.tick() is TickOutcome.STARTED
        finally:
            p2.stop()

    def test_model_window_checked_for_fable_jobs(self, harness, tmp_path):
        entries = {
            "1": _entry(10, 10, now=NOW, scoped={"Fable": 95}),
            "2": _entry(10, 10, now=NOW, scoped={"Fable": 95}),
        }
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True)
        _queue(store, tmp_path, model="claude-fable-5-1")
        try:
            assert engine.tick() is TickOutcome.HELD
        finally:
            p.stop()

    def test_stale_usage_is_not_trusted(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW, age=3600), "2": _entry(10, 10, now=NOW, age=3600)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True)
        _queue(store, tmp_path)
        try:
            assert engine.tick() is TickOutcome.HELD
        finally:
            p.stop()
        assert events[-1].reason == "no-capacity"

    def test_manual_jobs_are_skipped(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(10, 10, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True)
        _queue(store, tmp_path, auto=False)
        try:
            assert engine.tick() is TickOutcome.NOTHING
        finally:
            p.stop()

    def test_error_event_on_exception(self, harness, tmp_path):
        engine, store, events, p = _engine(harness, tmp_path, entries={})
        _queue(store, tmp_path)
        p.stop()
        with patch.object(harness.switcher, "usage_entries_by_account", side_effect=RuntimeError("x")):
            assert engine.tick() is TickOutcome.ERROR
        assert isinstance(events[-1], ErrorEvent)

    def test_events_serialize(self, harness, tmp_path):
        entries = {"1": _entry(10, 10, now=NOW), "2": _entry(10, 10, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True)
        _queue(store, tmp_path)
        try:
            engine.tick()
        finally:
            p.stop()
        for e in events:
            json.dumps(e.to_json())
            assert e.human()


class TestPooledDecisions:
    """The pool decides affordability; a single account must hold the job."""

    def _engine(self, harness, tmp_path, entries, **settings):
        events: list = []
        store = JobStore(harness.switcher.backup_dir)
        engine = JobsEngine(
            harness.switcher, JobsSettings(quiet_minutes=0, **settings), events.append,
            store=store, clock=harness.clock, dry_run=True, claude_home=tmp_path / "ch",
        )
        return engine, store, events, patch.object(
            harness.switcher, "usage_entries_by_account", return_value=entries)

    def test_reserve_no_longer_charged_twice(self, harness, tmp_path):
        """Both accounts 45% used with a 10% reserve. Per-account each has 45
        spare; the pool has 110 minus one reserve — a 50% job fits the pool
        and account 1 has the room to run it."""
        entries = {"1": _entry(0, 45, now=NOW), "2": _entry(0, 45, now=NOW)}
        engine, store, events, p = self._engine(harness, tmp_path, entries, weekly_reserve_pct=10)
        _queue(store, tmp_path, estimate_pct=5.0, weekly_estimate_pct=50.0)
        with p:
            assert engine.tick() is TickOutcome.STARTED

    def test_pool_blocks_when_together_they_cannot_afford_it(self, harness, tmp_path):
        entries = {"1": _entry(0, 95, now=NOW), "2": _entry(0, 95, now=NOW)}
        engine, store, events, p = self._engine(harness, tmp_path, entries, weekly_reserve_pct=10)
        _queue(store, tmp_path, estimate_pct=5.0, weekly_estimate_pct=30.0)
        with p:
            assert engine.tick() is TickOutcome.HELD

    def test_job_larger_than_any_single_account_is_held(self, harness, tmp_path):
        """The pool can afford 80 points, but no single account holds 80 —
        and a run cannot be split."""
        entries = {"1": _entry(0, 55, now=NOW), "2": _entry(0, 55, now=NOW)}
        engine, store, events, p = self._engine(harness, tmp_path, entries, weekly_reserve_pct=0)
        _queue(store, tmp_path, estimate_pct=5.0, weekly_estimate_pct=80.0)
        with p:
            assert engine.tick() is TickOutcome.HELD

    def test_exhausted_account_does_not_veto_the_other(self, harness, tmp_path):
        """Matt's 2026-09-14 shape: one weekly window spent, the other half
        free. Per-account both read -10 spare; pooled, account 1 can run."""
        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 100, now=NOW)}
        engine, store, events, p = self._engine(harness, tmp_path, entries, weekly_reserve_pct=10)
        _queue(store, tmp_path, estimate_pct=5.0, weekly_estimate_pct=8.0)
        with p:
            assert engine.tick() is TickOutcome.STARTED
        start = events[-1]
        assert isinstance(start, StartEvent) and start.account == "a@example.com"

    def test_pinned_job_judged_against_its_own_account(self, harness, tmp_path):
        entries = {"1": _entry(0, 20, now=NOW), "2": _entry(0, 100, now=NOW)}
        engine, store, events, p = self._engine(harness, tmp_path, entries, weekly_reserve_pct=0)
        _queue(store, tmp_path, account="2", estimate_pct=5.0, weekly_estimate_pct=20.0)
        with p:
            assert engine.tick() is TickOutcome.HELD

    def test_hold_names_the_next_refill(self, harness, tmp_path):
        entries = {"1": _entry(0, 99, now=NOW), "2": _entry(0, 99, now=NOW)}
        engine, store, events, p = self._engine(harness, tmp_path, entries, weekly_reserve_pct=10)
        _queue(store, tmp_path, estimate_pct=5.0, weekly_estimate_pct=30.0)
        with p:
            engine.tick()
        hold = events[-1]
        assert hold.reason == "no-capacity"
        assert "pooled spare" in hold.detail and "refills in" in hold.detail


class TestStaleBusyRecords:
    """A killed turn leaves `busy` behind; it must not block the queue."""

    def test_stale_busy_is_ignored(self, tmp_path):
        _session_file(tmp_path, os.getpid(), status="busy", updated_ms=(NOW - 3 * 3600) * 1000)
        rep = interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=())
        assert rep.busy == () and rep.stale_busy == 1
        assert rep.idle_s == 3 * 3600  # idle measured from that stamp

    def test_fresh_busy_still_blocks(self, tmp_path):
        _session_file(tmp_path, os.getpid(), status="busy", updated_ms=(NOW - 60) * 1000)
        rep = interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=())
        assert rep.idle_s == 0.0 and len(rep.busy) == 1 and rep.stale_busy == 0

    def test_boundary(self, tmp_path):
        from claude_swap.jobs_engine import BUSY_STALE_S

        _session_file(tmp_path, os.getpid(), status="busy", updated_ms=(NOW - BUSY_STALE_S + 30) * 1000)
        assert interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=()).idle_s == 0.0
        _session_file(tmp_path, os.getpid(), status="busy", updated_ms=(NOW - BUSY_STALE_S - 30) * 1000)
        assert interactive_idle(now=NOW, claude_home=tmp_path, profile_dirs=()).busy == ()

    def test_one_stale_does_not_mask_a_real_busy_session(self, tmp_path):
        home, prof = tmp_path / "home", tmp_path / "prof"
        _session_file(home, 4242, status="busy", updated_ms=(NOW - 90 * 3600) * 1000)
        _session_file(prof, os.getpid(), status="busy", updated_ms=(NOW - 30) * 1000)
        with patch("claude_swap.jobs_engine.scan_sessions", side_effect=lambda claude_dir: (
            [ClaudeSession(pid=os.getpid() if claude_dir == prof else 4242, session_id="s",
                           cwd="/tmp", started_at=0, kind="interactive", entrypoint="cli",
                           status="busy")], 0)):
            rep = interactive_idle(now=NOW, claude_home=home, profile_dirs=(prof,))
        assert len(rep.busy) == 1 and rep.stale_busy == 1
        assert rep.idle_s == 0.0

    def test_stale_busy_lets_a_job_start(self, harness, tmp_path):
        """The overnight failure, end to end."""
        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 100, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries, dry_run=True,
                                           settings=JobsSettings(quiet_minutes=20))
        _session_file(tmp_path / "claude-home", os.getpid(), status="busy",
                      updated_ms=(NOW - 46 * 3600) * 1000)
        _queue(store, tmp_path, estimate_pct=5.0)
        try:
            assert engine.tick() is TickOutcome.STARTED
        finally:
            p.stop()


class TestCycleRecording:
    def test_capacities_records_completed_cycles(self, harness, tmp_path):
        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 50, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        try:
            with patch("claude_swap.cycles.update_from_history", return_value=1) as rec:
                engine.capacities(now=NOW)
        finally:
            p.stop()
        rec.assert_called_once()
        assert set(rec.call_args.args[2]) == {"a@example.com", "b@example.com"}

    def test_recording_failure_never_breaks_a_tick(self, harness, tmp_path):
        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 50, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        try:
            with patch("claude_swap.cycles.update_from_history", side_effect=RuntimeError("disk full")):
                caps = engine.capacities(now=NOW)
        finally:
            p.stop()
        assert len(caps) == 2


class TestLearnedReserveApplied:
    def test_floor_used_until_enough_cycles(self, harness, tmp_path):
        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 50, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        try:
            learned = engine.learned_reserve()
        finally:
            p.stop()
        assert learned.pct == engine.settings.weekly_reserve_pct
        assert learned.learned is False

    def test_learned_value_lowers_the_reserve(self, harness, tmp_path):
        from claude_swap.learned import MIN_CYCLES, LearnedReserve

        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 50, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        low = LearnedReserve(pct=3.0, cycles=MIN_CYCLES, own_demand_pct=92.0,
                             floor_pct=10.0, reason="learned")
        try:
            with patch.object(engine, "learned_reserve", return_value=low):
                caps = engine.capacities(now=NOW)
        finally:
            p.stop()
        assert caps[0].window("7d").reserve_pct == 3.0

    def test_disabled_by_setting(self, harness, tmp_path):
        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 50, now=NOW)}
        engine, store, events, p = _engine(
            harness, tmp_path, entries=entries,
            settings=JobsSettings(quiet_minutes=0, learn_reserve=False))
        try:
            learned = engine.learned_reserve()
        finally:
            p.stop()
        assert learned.reason == "learning disabled"
        assert learned.pct == engine.settings.weekly_reserve_pct

    def test_failure_falls_back_to_the_floor(self, harness, tmp_path):
        entries = {"1": _entry(10, 42, now=NOW), "2": _entry(0, 50, now=NOW)}
        engine, store, events, p = _engine(harness, tmp_path, entries=entries)
        try:
            with patch("claude_swap.cycles.CycleStore", side_effect=RuntimeError("boom")):
                learned = engine.learned_reserve()
        finally:
            p.stop()
        assert learned.reason == "unavailable"
        assert learned.pct == engine.settings.weekly_reserve_pct
